"""UCP 2026-08-25 REST and MCP wire translation for active merchant adapters.

Authoritative sources (retrieved 2026-09-09):
https://ucp.dev/specification/shopping/catalog/rest/
https://ucp.dev/specification/shopping/cart/rest/
https://ucp.dev/specification/shopping/checkout/rest/
https://ucp.dev/2026-08-25/schemas/shopping/cart.json

Unsupported optional request features are rejected, never silently dropped. The
host authenticates, negotiates profiles, enforces policy, binds ownership and
idempotency before dispatching normalized operations.
"""

from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

VERSION = "2026-08-25"
ROOT = f"https://ucp.dev/{VERSION}"
CAPABILITIES = {
    "catalog.search": ("shopping/catalog/search", "catalog_search"),
    "catalog.lookup": ("shopping/catalog/lookup", "catalog_lookup"),
    "cart": ("shopping/cart", "cart"),
    "checkout": ("shopping/checkout", "checkout"),
    "order": ("shopping/order", "order"),
    "discount": ("shopping/extensions/discount", "discount"),
    "fulfillment": ("shopping/extensions/fulfillment", "fulfillment"),
}
REQUIREMENTS = {
    "catalog.search": {"search_products"},
    "catalog.lookup": {"get_product", "lookup_products"},
    "cart": {"create_cart", "get_cart", "replace_cart_items", "cancel_cart"},
    # Do not advertise a complete checkout capability on the basis of URL handoff.
    "checkout": {"create_checkout", "get_checkout", "update_checkout", "complete_checkout", "cancel_checkout", "get_order"},
    "order": {"get_order"},
    "discount": {"apply_discount_code", "remove_discount_code"},
    "fulfillment": {"get_shipping_options", "set_shipping_address", "select_shipping_option"},
}
OP_CAP = {
    "search_products": "catalog.search",
    "lookup_products": "catalog.lookup",
    "get_product": "catalog.lookup",
    **{
        op: "cart"
        for op in (
            "create_cart",
            "get_cart",
            "replace_cart_items",
            "cancel_cart",
            "add_to_cart",
            "update_cart_item",
            "remove_from_cart",
        )
    },
    "create_checkout": "checkout",
    "get_checkout": "checkout",
    "update_checkout": "checkout",
    "complete_checkout": "checkout",
    "cancel_checkout": "checkout",
    "get_order": "order",
}


def discovery(
    base_url: str,
    store_domain: str,
    capabilities,
    *,
    allow_local_http: bool = False,
    mcp_endpoint: str | None = None,
    payment_handlers: dict | None = None,
    identity_linking: dict | None = None,
) -> dict:
    """Accept validated canonical operation names, never inferred ready flags."""
    url = urlsplit(base_url)
    local_http = allow_local_http and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (url.scheme != "https" and not local_http) or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Published UCP endpoint must be HTTPS without credentials/query/fragment")
    if not store_domain or any(char in store_domain for char in "/:@?#"):
        raise ValueError("Store domain must be a hostname")
    supported = set(capabilities)
    declarations = {}
    for capability, required in REQUIREMENTS.items():
        if required <= supported:
            spec, schema = CAPABILITIES[capability]
            declaration = {
                "version": VERSION,
                "spec": f"{ROOT}/specification/{spec}",
                "schema": f"{ROOT}/schemas/shopping/{schema}.json",
            }
            if capability in {"discount", "fulfillment"}:
                declaration.update(
                    extends=["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"],
                    requires={"protocol": {"min": VERSION}},
                )
            if capability == "fulfillment":
                declaration["config"] = {"multi_destination": [], "method_combinations": [["shipping"]]}
            declarations[f"dev.ucp.shopping.{capability}"] = [declaration]
    if identity_linking:
        declarations["dev.ucp.common.identity_linking"] = [{
            "version": VERSION,
            "spec": f"{ROOT}/specification/common/identity-linking",
            "schema": f"{ROOT}/schemas/common/identity_linking.json",
            "config": identity_linking,
        }]
    services = [
        {
            "version": VERSION,
            "spec": f"{ROOT}/specification/overview",
            "transport": "rest",
            "schema": f"{ROOT}/services/shopping/rest.openapi.json",
            "endpoint": base_url.rstrip("/"),
        }
    ]
    if mcp_endpoint:
        mcp_url = urlsplit(mcp_endpoint)
        mcp_local = allow_local_http and mcp_url.scheme == "http" and mcp_url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (mcp_url.scheme != "https" and not mcp_local) or not mcp_url.hostname or mcp_url.username or mcp_url.password or mcp_url.query or mcp_url.fragment:
            raise ValueError("Published UCP MCP endpoint must be HTTPS without credentials/query/fragment")
        services.insert(0, {
            "version": VERSION,
            "spec": f"{ROOT}/specification/overview",
            "transport": "mcp",
            "schema": f"{ROOT}/services/shopping/mcp.openrpc.json",
            "endpoint": mcp_endpoint.rstrip("/"),
        })
    return {
        "ucp": {
            "version": VERSION,
            "services": {"dev.ucp.shopping": services},
            "capabilities": declarations,
            "payment_handlers": payment_handlers or {},
        }
    }


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemID(Strict):
    id: str = Field(min_length=1, max_length=200)


class Line(Strict):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    item: ItemID
    quantity: int = Field(ge=1, le=10000, strict=True)


class Lines(Strict):
    line_items: list[Line] = Field(max_length=1000)
    discounts: dict | None = None


class Pagination(Strict):
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class Search(Strict):
    query: str = Field(default="", max_length=500)
    pagination: Pagination = Field(default_factory=Pagination)


class Lookup(Strict):
    ids: list[str] = Field(min_length=1, max_length=100)


class CheckoutCreate(Strict):
    cart_id: str | None = Field(default=None, min_length=1, max_length=200)
    line_items: list[Line] | None = Field(default=None, max_length=1000)
    buyer: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    signals: dict = Field(default_factory=dict)
    attribution: dict = Field(default_factory=dict)
    payment: dict = Field(default_factory=dict)
    discounts: dict | None = None
    fulfillment: dict = Field(default_factory=dict)


class CheckoutUpdate(Strict):
    line_items: list[Line] = Field(max_length=1000)
    buyer: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    fulfillment: dict = Field(default_factory=dict)
    discounts: dict | None = None


class CheckoutComplete(Strict):
    payment: dict


def validate_request(operation: str, body: dict) -> dict:
    """Normalize supported wire requests. Root binds route resource IDs separately.

    Lookup returns ids for host fan-out; result correlation must use these exact IDs.
    Checkout supports official cart-to-checkout extension; line_items-only returns
    items for an explicitly idempotent host workflow, not hidden connector calls.
    """
    if operation == "search_products":
        request = Search.model_validate(body)
        return {"query": request.query, "limit": request.pagination.limit}
    if operation == "lookup_products":
        request = Lookup.model_validate(body)
        if any(not i or len(i) > 200 for i in request.ids):
            raise ValueError("Lookup identifier is invalid")
        return {"ids": request.ids}
    if operation == "get_product":
        return {"product_id": ItemID.model_validate(body).id}
    if operation in {"get_cart", "get_checkout", "cancel_cart"}:
        Strict.model_validate(body)
        return {}
    if operation in {"create_cart", "replace_cart_items"}:
        request = Lines.model_validate(body)
        if len({i.item.id for i in request.line_items}) != len(request.line_items):
            raise ValueError("Duplicate item identifiers are not supported")
        codes = request.discounts.get("codes", []) if request.discounts is not None else None
        if codes is not None and (not isinstance(codes, list) or any(not isinstance(code, str) or not code or len(code) > 200 for code in codes)):
            raise ValueError("Discount codes are invalid")
        result = {"items": [{"product_id": i.item.id, "quantity": i.quantity} for i in request.line_items]}
        if codes is not None:
            result["_discount_codes"] = codes
        return result
    if operation == "create_checkout":
        request = CheckoutCreate.model_validate(body)
        if request.cart_id:
            # Required by cart extension: ignore overlapping line_items.
            return {"cart_id": request.cart_id}
        if request.line_items is None:
            raise ValueError("Checkout requires line_items or the negotiated cart_id extension")
        result = {
            "items": [{"product_id": i.item.id, "quantity": i.quantity} for i in request.line_items],
            "buyer": request.buyer,
            "context": {**request.context, "signals": request.signals, "attribution": request.attribution},
            "payment": request.payment,
            "fulfillment": request.fulfillment,
        }
        if request.discounts is not None:
            result["_discount_codes"] = request.discounts.get("codes", [])
        return result
    if operation == "update_checkout":
        request = CheckoutUpdate.model_validate(body.get("checkout", body))
        result = {
            "items": [{"product_id": i.item.id, "quantity": i.quantity} for i in request.line_items],
            "buyer": request.buyer,
            "context": request.context,
            "fulfillment": request.fulfillment,
        }
        if request.discounts is not None:
            result["_discount_codes"] = request.discounts.get("codes", [])
        return result
    if operation == "complete_checkout":
        request = CheckoutComplete.model_validate(body.get("checkout", body))
        return {"payment": request.payment}
    if operation in {"cancel_checkout", "get_order"}:
        Strict.model_validate({})
        return {}
    raise ValueError("Unsupported UCP operation")


def _minor(value, currency):
    # Canonical SDK is currently a two-decimal schema. Do not misprice JPY/KWD.
    if currency not in {"USD", "EUR", "GBP", "ILS", "CAD", "AUD"}:
        raise ValueError("Unsupported currency exponent")
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value() or amount > 9007199254740991:
        raise ValueError("Invalid exact minor-unit amount")
    return int(amount)


def _public_url(value, *, allow_local_http=False):
    parsed = urlsplit(value)
    local = allow_local_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Public merchant link must be HTTPS without credentials")
    return value


def _product(product, lookup_ids=None, *, allow_local_http=False):
    currency = product["currency"]
    price = {"amount": _minor(product["price"], currency), "currency": currency}
    description = {"plain": product.get("description") or ""}
    variants = product.get("variants") or [
        {
            "id": product["id"],
            "sku": product["sku"],
            "title": product["title"],
            "price": product["price"],
            "availability": product.get("availability"),
        }
    ]
    normalized = []
    for variant in variants:
        # Explicit normalized variant semantics; arbitrary merchant metadata is not wire data.
        result = {
            "id": variant["id"],
            "title": variant.get("title", product["title"]),
            "description": {"plain": variant.get("description") or product.get("description") or ""},
            "price": {"amount": _minor(variant["price"], currency), "currency": currency},
        }
        if variant.get("sku"):
            result["sku"] = variant["sku"]
        availability = variant.get("availability", product.get("availability"))
        if availability in {"in_stock", "out_of_stock"}:
            result["availability"] = {"available": availability == "in_stock"}
        if lookup_ids is not None:
            correlations = [
                {"id": i, "match": "exact" if i == variant["id"] else "featured"}
                for i in lookup_ids
                if i in {variant["id"], product["id"]}
            ]
            if not correlations:
                continue
            result["inputs"] = correlations
        normalized.append(result)
    if not normalized:
        raise ValueError("Product does not resolve the requested identifier")
    prices = [v["price"]["amount"] for v in normalized]
    result = {
        "id": product["id"],
        "title": product["title"],
        "description": description,
        "price_range": {"min": {**price, "amount": min(prices)}, "max": {**price, "amount": max(prices)}},
        "variants": normalized,
    }
    if product.get("product_url"):
        result["url"] = _public_url(product["product_url"], allow_local_http=allow_local_http)
    if product.get("images"):
        result["media"] = [{"type": "image", "url": _public_url(url, allow_local_http=allow_local_http)} for url in product["images"]]
    return result


def _cart(cart):
    currency = cart["currency"]
    lines = []
    for index, item in enumerate(cart["items"]):
        quantity = item["quantity"]
        if type(quantity) is not int or quantity < 1:
            raise ValueError("Invalid cart quantity")
        lines.append(
            {
                "id": item.get("id") or f"line-{index}",
                "item": {
                    "id": item.get("variant_id") or item["product_id"],
                    "title": item.get("title") or item["sku"],
                    "price": _minor(item["unit_price"], currency),
                },
                "quantity": quantity,
                "totals": [
                    {"type": "subtotal", "amount": _minor(Decimal(str(item["unit_price"])) * quantity, currency)},
                    {"type": "total", "amount": _minor(item["total_price"], currency)},
                ],
            }
        )
    totals = [{"type": key, "amount": _minor(cart[key], currency)} for key in ("subtotal", "tax", "total")]
    discount = _minor(cart.get("discounts", "0"), currency)
    if discount:
        totals.insert(1, {"type": "discount", "amount": -discount})
    body = {"id": cart["id"], "currency": currency, "line_items": lines, "totals": totals}
    codes = cart.get("metadata", {}).get("discount_codes", [])
    if codes or discount:
        # A connector may report an aggregate discount without a per-code
        # allocation.  Do not invent that allocation: UCP's ``applied`` list
        # is optional and is emitted only when the merchant supplies it.
        body["discounts"] = {"codes": codes}
        applied = cart.get("metadata", {}).get("applied_discounts")
        if isinstance(applied, list):
            body["discounts"]["applied"] = applied
    return body


def _order(order, *, allow_local_http=False):
    cart = {
        "id": order["id"], "items": order.get("items", []), "subtotal": order["total"],
        "discounts": "0", "tax": "0", "total": order["total"], "currency": order["currency"],
    }
    body = _cart(cart)
    body.update(checkout_id=order["checkout_id"], status=order["status"])
    if order.get("order_url"):
        body["permalink_url"] = _public_url(order["order_url"], allow_local_http=allow_local_http)
    return body


def encode_response(operation: str, result, *, allow_local_http=False, payment_handlers=None) -> dict:
    """Convert already schema-validated canonical connector output to wire JSON.

    For lookup use {'products':[Product...], 'ids':[original IDs...]}. For checkout
    use {'checkout':Checkout, 'cart':authoritative Cart snapshot}; checkout URL is a
    human handoff and never represents completed payment.
    """
    capability = OP_CAP.get(operation)
    if capability is None:
        raise ValueError("Unsupported UCP response")
    metadata = {"version": VERSION, "capabilities": {f"dev.ucp.shopping.{capability}": [{"version": VERSION}]}}
    if operation == "search_products":
        body = {"products": [_product(p, allow_local_http=allow_local_http) for p in result]}
    elif operation == "lookup_products":
        body = {"products": [_product(p, result["ids"], allow_local_http=allow_local_http) for p in result["products"]]}
    elif operation == "get_product":
        body = {"product": _product(result, allow_local_http=allow_local_http)}
    elif operation in {"create_checkout", "get_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}:
        checkout, cart = result["checkout"], result["cart"]
        if checkout["cart_id"] != cart["id"] or checkout["currency"] != cart["currency"]:
            raise ValueError("Checkout and cart context mismatch")
        body = _cart(cart)
        status = checkout.get("status", "requires_escalation")
        if checkout.get("checkout_url") and not payment_handlers and status not in {"completed", "canceled"}:
            # Payment handoff is an explicit escalation state: a checkout that
            # still needs buyer action on the merchant site is never reported as
            # incomplete-but-completable, and never as completed.
            status = "requires_escalation"
        body.update(id=checkout["id"], status=status, links=[])
        body["totals"][-1]["amount"] = _minor(checkout["total"], checkout["currency"])
        if checkout.get("checkout_url") and status != "completed":
            body["continue_url"] = _public_url(checkout["checkout_url"], allow_local_http=allow_local_http)
        elif status not in {"completed", "canceled"}:
            raise ValueError("Checkout handoff requires a merchant checkout URL")
        body["messages"] = [] if status in {"ready_for_complete", "completed", "canceled"} else [
            {
                "type": "error",
                "code": "requires_buyer_input",
                "content": "Complete checkout on the merchant website; autonomous payment is not enabled.",
                "severity": "requires_buyer_input",
            }
        ]
        fulfillment = checkout.get("shipping_context", {}).get("fulfillment")
        if fulfillment:
            body["fulfillment"] = fulfillment
        if status == "completed":
            order = result.get("order")
            if not order or not order.get("order_url"):
                raise ValueError("Completed checkout requires an order confirmation and permalink")
            body["order"] = {
                "id": order["id"],
                "permalink_url": _public_url(order["order_url"], allow_local_http=allow_local_http),
            }
        metadata["payment_handlers"] = payment_handlers or {}
    elif operation == "get_order":
        body = _order(result, allow_local_http=allow_local_http)
    else:
        body = _cart(result)
    return {"ucp": metadata, **body}
