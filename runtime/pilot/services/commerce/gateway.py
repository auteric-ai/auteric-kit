"""Authenticated agent front door: policy, exact approvals and outbound execution.

Known-client bearer credentials are identity; UCP-Agent profile headers alone are not.
No human storefront traffic is intercepted by these explicitly scoped routes.
"""

import asyncio
import json
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

from auteric_commerce.actions import CommerceAction
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .storage import digest, encode, uid
from .ucp import VERSION, encode_response, validate_request

READS = {"search_products", "get_product", "get_cart", "get_checkout", "get_order", "get_shipping_options", "lookup_products"}
ACTION_TYPES = {
    "search_products": "catalog.search",
    "lookup_products": "catalog.search",
    "get_product": "product.read",
    "create_cart": "cart.create",
    "get_cart": "cart.read",
    "add_to_cart": "cart.add_item",
    "update_cart_item": "cart.update_item",
    "remove_from_cart": "cart.remove_item",
    "replace_cart_items": "cart.update",
    "cancel_cart": "cart.cancel",
    "create_checkout": "checkout.create",
    "get_checkout": "checkout.read",
    "update_checkout": "checkout.update",
    "complete_checkout": "checkout.complete",
    "cancel_checkout": "checkout.cancel",
    "get_order": "order.read",
    "apply_discount_code": "discount.apply",
    "remove_discount_code": "discount.remove",
    "get_shipping_options": "fulfillment.options.read",
    "set_shipping_address": "fulfillment.address.set",
    "select_shipping_option": "fulfillment.option.select",
}


def policy_product_projection(product, product_id):
    """Project either supported merchant product shape into policy context.

    Native HTTP returns MEP/1 products with variant prices in minor units. The
    legacy worker and generated adapters return the canonical SDK shape with a
    product-level decimal price. Policy is deliberately transport-neutral: it
    must preserve authoritative fields from either shape rather than replacing
    a valid legacy product with empty Native-only defaults.
    """
    if not isinstance(product, dict):
        return {"id": product_id}

    variants = [value for value in product.get("variants", []) if isinstance(value, dict)]
    first_variant = next((value for value in variants if value.get("price") is not None), None)
    direct_price = product.get("price")
    variant_price = first_variant.get("price") if first_variant else None
    native_price_source = direct_price if isinstance(direct_price, dict) else variant_price
    native_price = native_price_source.get("amount_minor") if isinstance(native_price_source, dict) else None
    metadata = product.get("metadata") if isinstance(product.get("metadata"), dict) else {}
    categories = metadata.get("categories")
    if not isinstance(categories, list):
        categories = product.get("tags", [])

    if native_price is not None:
        price = native_price / 100
    else:
        price = variant_price if first_variant else direct_price
    currency = (
        direct_price.get("currency") if isinstance(direct_price, dict)
        else variant_price.get("currency") if isinstance(variant_price, dict)
        else first_variant.get("currency") if first_variant else product.get("currency")
    )
    availability = (
        "in_stock" if product.get("available") is True
        else "out_of_stock" if product.get("available") is False
        else "in_stock" if any(value.get("available", value.get("availability") == "in_stock") for value in variants)
        else "out_of_stock" if variants
        else product.get("availability")
    )
    return {
        **product,
        "id": product_id,
        "currency": currency,
        "price": price,
        "availability": availability,
        "metadata": {**metadata, "categories": categories},
    }


def redact(value):
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if re.search(
                    r"token|secret|password|authorization|cookie|payment|customer|shipping|email|card", k, re.I
                )
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        return urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
    return value


def result_identity(result, operation=None):
    """Return the resource identifier appropriate to the canonical operation.

    A checkout contains both cart_id and checkout_id.  The former must never
    be selected when recording or validating a checkout resource.
    """
    if not isinstance(result, dict):
        return None
    if operation in {"create_checkout", "get_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}:
        return result.get("checkout_id") or result.get("id")
    if operation in {"create_cart", "get_cart", "add_to_cart", "update_cart_item", "remove_from_cart", "replace_cart_items", "cancel_cart"}:
        return result.get("cart_id") or result.get("id")
    if operation == "get_order":
        return result.get("order_id") or result.get("id")
    if operation in {"get_product", "search_products"}:
        return result.get("product_id") or result.get("id")
    return result.get("id") or result.get("cart_id") or result.get("checkout_id") or result.get("order_id") or result.get("product_id")


def policy_checkout_projection(checkout, checkout_id):
    """Give policy the stable `id` field while retaining Native HTTP fields."""
    if not isinstance(checkout, dict):
        return {"id": checkout_id}
    return {**checkout, "id": checkout_id}


def attach_gateway(app, dbs, base, development):
    state = app.state
    api = "/api/commerce/stores/{store_id}"

    def agent(request, store_id):
        credential = state.auth_token(request, "agent", store_id)
        state.require_agent_access(store_id)
        session = request.headers.get("x-auteric-session", "")
        with dbs.db() as db:
            row = db.execute(
                "SELECT * FROM agent_sessions WHERE hash=? AND store=? AND agent=? AND expires>?",
                (digest(session), store_id, credential["hash"], time.time()),
            ).fetchone()
        if not row:
            raise HTTPException(401, "Create an authenticated agent session and send X-Auteric-Session")
        return credential["hash"], row["hash"]

    @app.post("/ucp/{store_id}/sessions")
    @app.post(api + "/agent-sessions")
    def session(store_id: str, request: Request):
        credential = state.auth_token(request, "agent", store_id)
        state.require_agent_access(store_id)
        if not dbs.limit("sessions:" + credential["hash"], 20):
            raise HTTPException(429, "Session creation rate exceeded")
        token = secrets.token_urlsafe(32)
        with dbs.db() as db:
            db.execute(
                "INSERT INTO agent_sessions VALUES(?,?,?,?)",
                (digest(token), store_id, credential["hash"], time.time() + 3600),
            )
        return {
            "session_id": token,
            "expires_in": 3600,
            "identity": "pre-authorized merchant client; not platform-signature verification",
        }

    def public_store_id(hostname):
        store = state.resolve_public_store(hostname)
        if not store["agent_access_enabled"]:
            raise HTTPException(409, "Agent access is not active for this merchant")
        return store["id"]

    @app.post("/agent-commerce/{hostname}/sessions")
    def public_session(hostname: str, request: Request):
        return session(public_store_id(hostname), request)

    def owns_resource(store_id, kind, resource_id, agent_id, session_id):
        with dbs.db() as db:
            row = db.execute(
                "SELECT 1 FROM resources WHERE store=? AND kind=? AND id=? AND agent=? AND session=?",
                (store_id, kind, resource_id, agent_id, session_id),
            ).fetchone()
        if not row:
            raise HTTPException(403, "Resource is not bound to this authenticated agent session")

    async def execute_action(
        store_id, operation, data, agent_id, session_id, request_key, *, resume_id=None, operator_id=None,
        protocol="UCP", executor=None, bootstrap_scope=False
    ):
        store = state.store_row(store_id)
        bootstrap_binding = (bootstrap_scope if isinstance(bootstrap_scope, str) else state.connection_binding(store_id)) if bootstrap_scope else None
        if not operator_id:
            state.require_agent_access(store_id)
        if store["environment"] == "production" and (not store["verified"] or store["verified"] < time.time() - 86400):
            raise HTTPException(409, "Verify current merchant-domain discovery before production agent traffic")
        if operation not in state.inputs:
            raise HTTPException(422, "Unsupported canonical operation")
        if not state.operation_enabled(store_id, operation) and not (operator_id and bootstrap_scope):
            raise HTTPException(403, "This commerce capability is not enabled for agents")
        try:
            data = state.inputs[operation].model_validate(data).model_dump(mode="json")
        except Exception:
            raise HTTPException(422, "Request does not satisfy the canonical commerce schema") from None
        action_id = digest(store_id + agent_id + session_id + request_key)
        if resume_id and resume_id != action_id:
            raise HTTPException(403, "Approval does not belong to this exact request")
        fingerprint = digest(encode({"operation": operation, "input": data}))
        with dbs.db() as db:
            previous = db.execute("SELECT * FROM traffic WHERE id=? AND store=?", (action_id, store_id)).fetchone()
        if previous:
            previous_fingerprint = (
                json.loads(previous["input"]).get("sensitive_digest")
                if previous["operation"] == "complete_checkout"
                else digest(encode({"operation": previous["operation"], "input": json.loads(previous["input"])}))
            )
            if previous_fingerprint != fingerprint:
                raise HTTPException(409, "Idempotency key reused for a changed action")
            if previous["state"] == "executed":
                # Sanitized telemetry isn't the replay receipt. Read durable connector output.
                with dbs.db() as db:
                    receipt = db.execute(
                        "SELECT result FROM jobs WHERE id=? AND state='completed'", (action_id,)
                    ).fetchone()
                if not receipt:
                    # Native HTTP has no outbound connector job: its signed
                    # merchant result is committed directly to `traffic` only
                    # after dispatch completes. Replaying that durable result
                    # is safe and is required to preserve the exact same
                    # idempotency semantics as the legacy worker path.
                    if str(previous["mapping_version"] or "").startswith("native:"):
                        from .execution_receipts import gateway_receipt
                        with dbs.db() as db:
                            exact = gateway_receipt(db, store_id, action_id)
                        if not exact or exact["operation"] != operation:
                            raise HTTPException(409, "Execution requires an exact durable receipt; no automatic retry")
                        return {
                            "action_id": action_id,
                            "state": "executed",
                            "result": json.loads(exact["result"]),
                            "replayed": True,
                        }
                    raise HTTPException(409, "Execution requires reconciliation; no automatic retry")
                return {
                    "action_id": action_id,
                    "state": "executed",
                    "result": json.loads(receipt["result"]),
                    "replayed": True,
                }
            if previous["state"] != "waiting_for_approval":
                raise HTTPException(409, "Action already evaluated or has unresolved execution; inspect activity")
        policy = state.policy_class.model_validate_json(store["policy"])
        if not dbs.limit("agent:" + store_id + agent_id, policy.max_requests_per_minute):
            dbs.event(store["org"], store_id, agent_id[:12], "action.blocked", {"reason": "rate-limit"})
            raise HTTPException(429, "Agent rate limit exceeded")
        mapping = state.selected_mapping(store_id, operation=operation)
        state.validate_runtime_mapping(store, mapping)
        binding = {"mapping": mapping["id"], "policy": digest(store["policy"]), "store": store_id}
        runtime = state.runtime_class(dbs.runtime_target, store_id, None, binding=binding)
        resource = data.get("cart_id") or data.get("checkout_id") or data.get("order_id")
        if resource:
            kind = "order" if "order_id" in data else "checkout" if "checkout_id" in data else "cart"
            owns_resource(store_id, kind, resource, agent_id, session_id)
        lock = "action:" + action_id if operation in READS else resource or "session:" + session_id
        if lock:
            try:
                with dbs.db() as db:
                    db.execute("INSERT INTO resource_locks VALUES(?,?,?)", (store_id, lock, action_id))
            except dbs.integrity_errors:
                raise HTTPException(
                    409, "An action for this resource is already running or needs reconciliation"
                ) from None
        started = time.monotonic()
        keep_lock = False
        try:
            if not previous:
                with dbs.db() as db:
                    db.execute(
                        "INSERT INTO traffic VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            action_id,
                            store_id,
                            agent_id,
                            session_id,
                            operation,
                            encode({"sensitive_digest": fingerprint}) if operation == "complete_checkout" else encode(data),
                            "{}",
                            "evaluating",
                            None,
                            None,
                            None,
                            time.time(),
                            mapping["id"],
                        ),
                    )
                dbs.event(store['org'], store_id, agent_id[:12], 'action.received',
                          {'action_id': action_id, 'operation': operation})
            context_maps = {}

            async def read_context(op, value):
                current = state.selected_mapping(store_id, operation=op)
                state.validate_runtime_mapping(state.store_row(store_id), current)
                context_maps[op] = current["id"]
                return await state.dispatch(store_id, op, value, current,
                                            principal="delegated-session:" + digest(encode([store_id, agent_id, session_id])),
                                            policy_exempt=bool(operator_id))

            async def context():
                ctx = {"agent_verified": True, "session_id": session_id, "products": {},
                       "agent_credential_id": agent_id, "operator_test": bool(operator_id)}
                if operation not in READS:
                    cart_id = data.get("cart_id")
                    if data.get("checkout_id"):
                        ctx["checkout"] = await read_context("get_checkout", {"checkout_id": data["checkout_id"]})
                        if result_identity(ctx["checkout"], "get_checkout") != data["checkout_id"]:
                            raise HTTPException(502, "Merchant returned a different checkout")
                        cart_id = ctx["checkout"].get("cart_id")
                        ctx["checkout"] = policy_checkout_projection(ctx["checkout"], data["checkout_id"])
                    if cart_id:
                        ctx["cart"] = await read_context("get_cart", {"cart_id": cart_id})
                        if result_identity(ctx["cart"], "get_cart") != cart_id:
                            raise HTTPException(502, "Merchant returned a different cart")
                        if "line_items" in ctx["cart"]:
                            canonical_cart = ctx["cart"]
                            ctx["cart"] = {
                                **canonical_cart,
                                "id": cart_id,
                                "items": [
                                    {
                                        "product_id": item["product_id"],
                                        "variant_id": item.get("variant_id"),
                                        "quantity": item["quantity"],
                                    }
                                    for item in canonical_cart.get("line_items", [])
                                ],
                                "total": canonical_cart.get("total", {}).get("amount_minor", 0) / 100,
                            }
                    # Worker carts use `items`; MEP/1 carts use `line_items`.
                    # Context is an internal policy projection, never the wire
                    # contract, so normalize just the identifiers it needs.
                    items = list(data.get("items", [])) + list(
                        ctx.get("cart", {}).get("items", ctx.get("cart", {}).get("line_items", []))
                    )
                    if data.get("product_id"):
                        items.append(data)
                    ids = {i["product_id"] for i in items}
                    for product_id in ids:
                        product = await read_context("get_product", {"product_id": product_id})
                        if result_identity(product, "get_product") != product_id:
                            raise HTTPException(502, "Merchant returned a different product")
                        ctx["products"][product_id] = policy_product_projection(product, product_id)
                    for item in items:
                        variant_id = item.get("variant_id")
                        if not variant_id:
                            continue
                        parent = ctx["products"][item["product_id"]]
                        variant = next((value for value in parent.get("variants", [])
                                        if value.get("id") == variant_id or value.get("variant_id") == variant_id), None)
                        if not variant:
                            raise HTTPException(409, "Selected product variant is unavailable")
                        ctx["products"][variant_id] = policy_product_projection(
                            {
                                **parent,
                                **variant,
                                "variants": [],
                                "metadata": {
                                    **parent.get("metadata", {}), **variant.get("metadata", {}),
                                    "parent_product_id": parent["id"],
                                },
                            },
                            variant_id,
                        )
                return ctx

            ctx = await context()
            decision = state.evaluate_policy(operation, data, policy, ctx)
            if previous:
                record = runtime.get_action(action_id)
                expected = record["action"]["merchant_context"]
                if expected != ctx or record["action"]["metadata"].get("context_mappings") != context_maps:
                    runtime.reject(action_id, "stale-state-review")
                    raise HTTPException(409, "Merchant context changed after approval; evaluate a new request")
            else:
                action = CommerceAction(
                    action_id=action_id,
                    idempotency_key=action_id,
                    trace_id=action_id,
                    merchant_id=store_id,
                    type=ACTION_TYPES[operation],
                    principal={"subject": "delegated-session:" + digest(encode([store_id, agent_id, session_id])), "roles": ["storefront"]},
                    agent={"id": agent_id[:12]},
                    source="mcp-gateway" if protocol == "MCP" else "sidecar-gateway" if protocol == "SIDECAR" else "ucp-gateway",
                    protocol=protocol,
                    app_id=agent_id,
                    merchant_context=ctx,
                    metadata={
                        "input": {"sensitive": True, "digest": fingerprint} if operation == "complete_checkout" else data,
                        "operation": operation,
                        "session": session_id,
                        "context_mappings": context_maps,
                    },
                )
                record = runtime.record(action, decision)
            with dbs.db() as db:
                db.execute(
                    "UPDATE traffic SET decision=?,state=? WHERE id=?", (encode(decision), record["state"], action_id)
                )
            dbs.event(
                store["org"],
                store_id,
                agent_id[:12],
                "policy.evaluated",
                {"action_id": action_id, "decision": decision["outcome"], "rules": decision["matched_rules"]},
            )
            if record["state"] in {"blocked", "waiting_for_approval"}:
                return {
                    "action_id": action_id,
                    "state": record["state"],
                    "digest": record["digest"],
                    "decision": decision,
                }

            async def revalidate():
                latest_store = state.store_row(store_id)
                state.validate_runtime_mapping(latest_store, mapping)
                if not operator_id:
                    state.require_agent_access(store_id)
                if bootstrap_scope and state.connection_binding(store_id) != bootstrap_binding:
                    raise HTTPException(409, "Bootstrap configuration changed before execution")
                if not state.operation_enabled(store_id, operation) and not (operator_id and bootstrap_scope):
                    raise HTTPException(403, "Capability was disabled before execution")
                if (
                    latest_store["policy"] != store["policy"]
                    or state.selected_mapping(store_id, operation=operation)["id"] != mapping["id"]
                ):
                    raise HTTPException(409, "Mapping/policy changed; evaluate a new action")
                with dbs.db() as db:
                    if operator_id:
                        valid = db.execute(
                            "SELECT 1 FROM users WHERE id=? AND org=?", (operator_id, store["org"])
                        ).fetchone()
                    else:
                        valid = state.credential_vault.is_active(agent_id, "mcp" if protocol == "MCP" else "agent", store_id)
                if not valid:
                    raise HTTPException(403, "Agent authority revoked")
                if not operator_id:
                    with dbs.db() as db:
                        session_valid = db.execute(
                            'SELECT 1 FROM agent_sessions WHERE hash=? AND store=? AND agent=? AND expires>?',
                            (session_id, store_id, agent_id, time.time()),
                        ).fetchone()
                    if not session_valid:
                        raise HTTPException(403, 'Agent session expired or revoked before execution')
                if operation not in READS:
                    evaluated_maps = dict(context_maps)
                    fresh = await context()
                    if fresh != ctx or context_maps != evaluated_maps:
                        raise HTTPException(409, "Authoritative state changed before execution")
                state.require_gateway_running()
                if not operator_id:
                    state.require_agent_access(store_id)

            async def perform():
                state.require_gateway_running()
                if not operator_id:
                    state.require_agent_access(store_id)
                    if not state.credential_vault.is_active(agent_id, 'mcp' if protocol == 'MCP' else 'agent', store_id):
                        raise HTTPException(403, 'Agent authority revoked before dispatch')
                    with dbs.db() as db:
                        valid = db.execute('SELECT 1 FROM agent_sessions WHERE hash=? AND store=? AND agent=? AND expires>?',
                                           (session_id, store_id, agent_id, time.time())).fetchone()
                    if not valid:
                        raise HTTPException(403, 'Session expired before dispatch')
                dispatch = executor or state.dispatch
                result = await dispatch(store_id, operation, data, mapping, key=action_id,
                                              principal="delegated-session:" + digest(encode([store_id, agent_id, session_id])),
                                              policy_exempt=bool(operator_id))
                if operation in {"get_product", "get_cart", "get_checkout", "get_order"}:
                    expected = data.get("product_id") or data.get("cart_id") or data.get("checkout_id") or data.get("order_id")
                    if result_identity(result, operation) != expected:
                        raise HTTPException(502, "Merchant response resource identity differs from requested identity")
                return result

            result = await runtime.run(action_id, agent_id[:12], perform, revalidate)
            if operation in {"get_product", "get_cart", "get_checkout", "get_order"}:
                expected = data.get("product_id") or data.get("cart_id") or data.get("checkout_id") or data.get("order_id")
                if result_identity(result, operation) != expected:
                    raise HTTPException(502, "Merchant response resource identity differs from requested identity")
            with dbs.db() as db:
                if operation in {"create_cart", "create_checkout"}:
                    db.execute(
                        "INSERT INTO resources VALUES(?,?,?,?,?)",
                        (
                            store_id,
                            "cart" if operation == "create_cart" else "checkout",
                            result_identity(result, operation),
                            agent_id,
                            session_id,
                        ),
                    )
                if operation == "complete_checkout" and result.get("metadata", {}).get("order_id"):
                    db.execute(
                        "INSERT INTO resources VALUES(?,?,?,?,?)",
                        (store_id, "order", result["metadata"]["order_id"], agent_id, session_id),
                    )
                db.execute(
                    "UPDATE traffic SET state='executed',response=?,latency=? WHERE id=?",
                    (encode(redact(result)), (time.monotonic() - started) * 1000, action_id),
                )
            dbs.event(
                store["org"],
                store_id,
                agent_id[:12],
                "action.executed",
                {"action_id": action_id, "mapping_version": mapping["id"]},
            )
            return {"action_id": action_id, "state": "executed", "result": result}
        except BaseException as exc:
            with dbs.db() as db:
                job = db.execute("SELECT state FROM jobs WHERE id=?", (action_id,)).fetchone()
                native = db.execute("SELECT outcome FROM execution_actions WHERE store_id=? AND action_id=?",
                    (store_id, action_id if action_id.startswith("action_") else "action_" + action_id)).fetchone()
                keep_lock = operation not in READS and bool(
                    (job and job["state"] in {"claimed", "uncertain", "completed"}) or
                    (native and native["outcome"] in {"reserved", "executing", "uncertain", "completed", "reconciled"})
                )
                db.execute(
                    "UPDATE traffic SET state=?,error=?,latency=? WHERE id=?",
                    (
                        "uncertain" if keep_lock else "failed",
                        encode(
                            {
                                "message": "Request not completed; inspect connector and policy evidence",
                                "uncertain": keep_lock,
                            }
                        ),
                        (time.monotonic() - started) * 1000,
                        action_id,
                    ),
                )
            dbs.event(store['org'], store_id, agent_id[:12], 'action.uncertain' if keep_lock else 'action.failed',
                      {'action_id': action_id, 'operation': operation, 'uncertain': keep_lock})
            if isinstance(exc, (HTTPException, asyncio.CancelledError)):
                raise
            from auteric_commerce.domain import DomainError

            if isinstance(exc, DomainError):
                raise HTTPException(exc.status, str(exc)) from None
            raise HTTPException(502, "Commerce operation failed safely; no automatic write retry") from None
        finally:
            if lock and not keep_lock:
                with dbs.db() as db:
                    db.execute(
                        "DELETE FROM resource_locks WHERE store=? AND resource=? AND action=?",
                        (store_id, lock, action_id),
                    )

    @app.post(api + "/actions/{operation}")
    async def canonical(store_id: str, operation: str, request: Request):
        aid, sid = agent(request, store_id)
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(422, "A stable Idempotency-Key is required")
        result = await execute_action(store_id, operation, await request.json(), aid, sid, key)
        return JSONResponse(
            result,
            status_code=403
            if result["state"] == "blocked"
            else 202
            if result["state"] == "waiting_for_approval"
            else 200,
        )

    class Approval(BaseModel):
        digest: str

    @app.post(api + "/approvals/{action_id}/approve")
    def approve(store_id: str, action_id: str, body: Approval, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        runtime = state.runtime_class(dbs.runtime_target, store_id, None)
        return runtime.approve(action_id, body.digest, actor["id"])

    @app.post(api + "/approvals/{action_id}/reject")
    def reject(store_id: str, action_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        runtime = state.runtime_class(dbs.runtime_target, store_id, None)
        result = runtime.reject(action_id, actor["id"])
        with dbs.db() as db:
            db.execute("UPDATE traffic SET state='rejected' WHERE id=? AND store=?", (action_id, store_id))
        return result

    @app.post(api + "/simulate")
    async def simulate(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        # Harmless, explicit operator simulation: reads only; no financial/cart mutations.
        result = await execute_action(
            store_id,
            "search_products",
            {"query": "", "limit": 5},
            "operator:" + actor["id"],
            "simulation:" + actor["id"],
            uid(),
            operator_id=actor["id"],
        )
        return {
            "mode": "read-only operator simulation",
            "state": result["state"],
            "result": result.get("result"),
            "production_ready": False,
        }
    from .connection_runs import attach_connection_runs
    from .onboarding_runs import attach_onboarding_runs

    attach_connection_runs(app, dbs, execute_action)
    attach_onboarding_runs(app, dbs)

    async def ucp_call(store_id, operation, request, resource_id=None):
        aid, sid = agent(request, store_id)
        with dbs.db() as db:
            store_mode = db.execute("SELECT environment FROM stores WHERE id=?", (store_id,)).fetchone()
        allow_local_http = bool(development and store_mode and store_mode["environment"] == "sandbox")
        if request.headers.get("ucp-version", VERSION) != VERSION:
            raise HTTPException(422, "Unsupported UCP version")
        # Discovery, MCP tool listing, and REST execution must agree: a
        # merchant-hosted checkout URL is a buyer handoff, not evidence that
        # Auteric may invoke a payment completion.  Do the refusal at the
        # public REST boundary as well, so callers cannot bypass the discovery
        # profile by guessing the lifecycle route.
        if operation == "complete_checkout":
            configuration = state.ucp_configuration(store_id)
            if not configuration["payment_handlers"] or not state.operation_enabled(store_id, "get_order"):
                raise HTTPException(
                    403,
                    "Autonomous checkout completion is not enabled; continue on the merchant website",
                )
        try:
            body = {} if request.method == "GET" else await request.json()
            data = validate_request(operation, body)
        except Exception:
            raise HTTPException(422, "Unsupported or invalid UCP request fields for this version") from None
        if resource_id:
            # Resource identifiers are part of the URL, never trusted from a
            # request body.  Keeping this mapping explicit prevents an order
            # read from accidentally being dispatched as a cart read and makes
            # all checkout lifecycle routes use the same canonical input.
            resource_key = {
                "get_cart": "cart_id",
                "replace_cart_items": "cart_id",
                "cancel_cart": "cart_id",
                "get_checkout": "checkout_id",
                "update_checkout": "checkout_id",
                "complete_checkout": "checkout_id",
                "cancel_checkout": "checkout_id",
                "get_order": "order_id",
            }.get(operation)
            if resource_key is None:
                raise HTTPException(500, "Route resource is not defined for this operation")
            data[resource_key] = resource_id
        key = request.headers.get("idempotency-key", "") or (uid() if operation in READS else "")
        if not key or len(key) > 200:
            raise HTTPException(422, "A stable Idempotency-Key is required for mutations")
        if operation == "lookup_products":
            products = []
            for index, product_id in enumerate(data["ids"]):
                envelope = await execute_action(
                    store_id, "get_product", {"product_id": product_id}, aid, sid, key + ":" + str(index)
                )
                if envelope["state"] != "executed":
                    return JSONResponse(envelope, status_code=403)
                products.append(envelope["result"])
            return encode_response(operation, {"ids": data["ids"], "products": products}, allow_local_http=allow_local_http)
        if operation == "create_checkout" and "items" in data:
            raise HTTPException(
                422,
                "This pilot requires the negotiated cart_id checkout extension; "
                "standalone line_items checkout is not enabled",
            )
        envelope = await execute_action(store_id, operation, data, aid, sid, key)
        if envelope["state"] != "executed":
            return JSONResponse(envelope, status_code=403 if envelope["state"] == "blocked" else 202)
        result = envelope["result"]
        if operation in {"create_checkout", "get_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}:
            cart = await execute_action(
                store_id, "get_cart", {"cart_id": result["cart_id"]}, aid, sid, key + ":cart-snapshot"
            )
            result = {"checkout": result, "cart": cart["result"]}
            # An actual completed checkout is represented only when the
            # merchant returned an order reference.  Fetch that order through
            # the same ownership/session path; never synthesize one from a
            # checkout handoff URL.
            if result["checkout"].get("status") == "completed":
                order_id = result["checkout"].get("metadata", {}).get("order_id")
                if not order_id:
                    raise HTTPException(502, "Completed checkout lacks a merchant order reference")
                order = await execute_action(
                    store_id, "get_order", {"order_id": order_id}, aid, sid, key + ":order-snapshot"
                )
                result["order"] = order["result"]
        try:
            return encode_response(operation, result, allow_local_http=allow_local_http)
        except Exception:
            raise HTTPException(
                502, "Canonical response cannot be represented safely in the negotiated UCP subset"
            ) from None

    @app.post("/ucp/{store_id}/catalog/search")
    async def search(store_id: str, request: Request):
        return await ucp_call(store_id, "search_products", request)

    @app.post("/ucp/{store_id}/catalog/lookup")
    async def lookup(store_id: str, request: Request):
        return await ucp_call(store_id, "lookup_products", request)

    @app.post("/ucp/{store_id}/catalog/product")
    async def product(store_id: str, request: Request):
        return await ucp_call(store_id, "get_product", request)

    @app.post("/ucp/{store_id}/carts")
    async def create_cart(store_id: str, request: Request):
        return await ucp_call(store_id, "create_cart", request)

    @app.get("/ucp/{store_id}/carts/{cart_id}")
    async def get_cart(store_id: str, cart_id: str, request: Request):
        return await ucp_call(store_id, "get_cart", request, cart_id)

    @app.put("/ucp/{store_id}/carts/{cart_id}")
    async def replace_cart(store_id: str, cart_id: str, request: Request):
        return await ucp_call(store_id, "replace_cart_items", request, cart_id)

    @app.post("/ucp/{store_id}/carts/{cart_id}/cancel")
    async def cancel_cart(store_id: str, cart_id: str, request: Request):
        return await ucp_call(store_id, "cancel_cart", request, cart_id)

    @app.post("/ucp/{store_id}/checkout-sessions")
    async def create_checkout(store_id: str, request: Request):
        return await ucp_call(store_id, "create_checkout", request)

    @app.get("/ucp/{store_id}/checkout-sessions/{checkout_id}")
    async def get_checkout(store_id: str, checkout_id: str, request: Request):
        return await ucp_call(store_id, "get_checkout", request, checkout_id)

    @app.put("/ucp/{store_id}/checkout-sessions/{checkout_id}")
    async def update_checkout(store_id: str, checkout_id: str, request: Request):
        return await ucp_call(store_id, "update_checkout", request, checkout_id)

    @app.post("/ucp/{store_id}/checkout-sessions/{checkout_id}/complete")
    async def complete_checkout(store_id: str, checkout_id: str, request: Request):
        return await ucp_call(store_id, "complete_checkout", request, checkout_id)

    @app.post("/ucp/{store_id}/checkout-sessions/{checkout_id}/cancel")
    async def cancel_checkout(store_id: str, checkout_id: str, request: Request):
        return await ucp_call(store_id, "cancel_checkout", request, checkout_id)

    @app.get("/ucp/{store_id}/orders/{order_id}")
    async def get_order(store_id: str, order_id: str, request: Request):
        return await ucp_call(store_id, "get_order", request, order_id)

    @app.post("/agent-commerce/{hostname}/catalog/search")
    async def public_search(hostname: str, request: Request):
        return await ucp_call(public_store_id(hostname), "search_products", request)

    @app.post("/agent-commerce/{hostname}/catalog/lookup")
    async def public_lookup(hostname: str, request: Request):
        return await ucp_call(public_store_id(hostname), "lookup_products", request)

    @app.post("/agent-commerce/{hostname}/catalog/product")
    async def public_product(hostname: str, request: Request):
        return await ucp_call(public_store_id(hostname), "get_product", request)

    @app.post("/agent-commerce/{hostname}/carts")
    async def public_create_cart(hostname: str, request: Request):
        return await ucp_call(public_store_id(hostname), "create_cart", request)

    @app.get("/agent-commerce/{hostname}/carts/{cart_id}")
    async def public_get_cart(hostname: str, cart_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "get_cart", request, cart_id)

    @app.put("/agent-commerce/{hostname}/carts/{cart_id}")
    async def public_replace_cart(hostname: str, cart_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "replace_cart_items", request, cart_id)

    @app.post("/agent-commerce/{hostname}/carts/{cart_id}/cancel")
    async def public_cancel_cart(hostname: str, cart_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "cancel_cart", request, cart_id)

    @app.post("/agent-commerce/{hostname}/checkout-sessions")
    async def public_create_checkout(hostname: str, request: Request):
        return await ucp_call(public_store_id(hostname), "create_checkout", request)

    @app.get("/agent-commerce/{hostname}/checkout-sessions/{checkout_id}")
    async def public_get_checkout(hostname: str, checkout_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "get_checkout", request, checkout_id)

    @app.put("/agent-commerce/{hostname}/checkout-sessions/{checkout_id}")
    async def public_update_checkout(hostname: str, checkout_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "update_checkout", request, checkout_id)

    @app.post("/agent-commerce/{hostname}/checkout-sessions/{checkout_id}/complete")
    async def public_complete_checkout(hostname: str, checkout_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "complete_checkout", request, checkout_id)

    @app.post("/agent-commerce/{hostname}/checkout-sessions/{checkout_id}/cancel")
    async def public_cancel_checkout(hostname: str, checkout_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "cancel_checkout", request, checkout_id)

    @app.get("/agent-commerce/{hostname}/orders/{order_id}")
    async def public_get_order(hostname: str, order_id: str, request: Request):
        return await ucp_call(public_store_id(hostname), "get_order", request, order_id)

    app.state.execute_action = execute_action
    app.state.gateway_agent = agent
