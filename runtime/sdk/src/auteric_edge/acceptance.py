"""Explicit, single-attempt connector acceptance; never a production certificate."""

from datetime import datetime, timezone

from .models import INPUTS, validate_input, validate_output

OPERATIONS = (
    "search_products",
    "get_product",
    "create_cart",
    "get_cart",
    "add_to_cart",
    "update_cart_item",
    "remove_from_cart",
    "create_checkout",
    "get_checkout",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


async def run_acceptance(
    connector,
    *,
    environment="sandbox",
    query="",
    product_id=None,
    variant_id=None,
    quantity=1,
    allow_sandbox_writes=False,
):
    """Use only caller-approved sandbox stock; any failure stops the journey.

    The caller must ensure credentials actually target a sandbox. The environment
    label cannot prove account isolation. No retries or automatic cleanup writes.
    """
    if environment not in {"sandbox", "staging", "production"}:
        raise ValueError("Unknown environment")
    if allow_sandbox_writes and environment != "sandbox":
        raise ValueError("Write acceptance requires an explicitly selected sandbox")
    declared = getattr(connector, "supported_operations", None)
    if not isinstance(declared, (set, frozenset, list, tuple)) or any(
        not isinstance(op, str) or op not in INPUTS for op in declared
    ):
        raise ValueError("Connector must declare valid supported_operations")
    if allow_sandbox_writes:
        if not product_id:
            raise ValueError("Explicit safe sandbox product_id required")
        validate_input(
            "add_to_cart",
            {
                "cart_id": "preflight",
                "product_id": product_id,
                "variant_id": variant_id,
                "quantity": quantity,
            },
        )
    report = {
        "started_at": _now(),
        "environment": environment,
        "production_ready": False,
        "scope": "sandbox_write_journey" if allow_sandbox_writes else "read_only",
        "operations": {op: {"status": "not_run", "attempts": []} for op in OPERATIONS},
        "stopped": False,
        "reconciliation_required": False,
    }
    for op in OPERATIONS:
        if op not in declared:
            report["operations"][op]["status"] = "unsupported"

    async def call(op, payload, verify=lambda result: True):
        row = report["operations"][op]
        if report["stopped"]:
            return None
        if op not in declared:
            report["stopped"] = True
            return None
        attempted = False
        event = {"started_at": _now()}
        try:
            request = validate_input(op, payload)
            attempted = True
            value = validate_output(op, await connector.execute(op, request))
            if not verify(value):
                raise ValueError("Acceptance postcondition failed")
            row["status"] = "passed"
            event["evidence"] = "canonical_schema_and_postconditions_valid"
            return value
        except Exception:
            row["status"] = "failed"
            event["evidence"] = "operation_failed_details_withheld"
            report["stopped"] = True
            if attempted and op not in {"search_products", "get_product", "get_cart", "get_checkout"}:
                report["reconciliation_required"] = True
            return None
        finally:
            event["finished_at"] = _now()
            row["attempts"].append(event)

    products = await call("search_products", {"query": query, "limit": 5})
    selected = product_id or (products[0]["id"] if products else None)
    product = await call("get_product", {"product_id": selected}, lambda p: p["id"] == selected) if selected else None
    if allow_sandbox_writes and product and not report["stopped"]:
        cart = await call("create_cart", {"currency": product["currency"]}, lambda c: not c["items"])
        if cart:
            cid = cart["id"]
            await call("get_cart", {"cart_id": cid}, lambda c: c["id"] == cid and not c["items"])
            item = {"cart_id": cid, "product_id": selected, "variant_id": variant_id, "quantity": quantity}

            def item_matches(c):
                return c["id"] == cid and any(
                    i["product_id"] == selected and i.get("variant_id") == variant_id and i["quantity"] == quantity
                    for i in c["items"]
                )

            await call("add_to_cart", item, item_matches)
            await call(
                "update_cart_item",
                {
                    "cart_id": cid,
                    "product_id": selected,
                    "quantity": quantity,
                },
                item_matches,
            )
            await call(
                "remove_from_cart",
                {"cart_id": cid, "product_id": selected},
                lambda c: c["id"] == cid and not c["items"],
            )
            # A checkout needs a nonempty cart; this is a distinct deliberate add,
            # never a retry of the previous write.
            await call("add_to_cart", item, item_matches)
            checkout = await call(
                "create_checkout", {"cart_id": cid}, lambda c: c["cart_id"] == cid and c["status"] != "completed"
            )
            if checkout:
                await call(
                    "get_checkout",
                    {"checkout_id": checkout["id"]},
                    lambda c: c["id"] == checkout["id"] and c["cart_id"] == cid and c["status"] != "completed",
                )
    report["finished_at"] = _now()
    return report
