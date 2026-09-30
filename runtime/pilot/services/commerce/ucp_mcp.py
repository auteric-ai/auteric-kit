"""UCP 2026-08-25 MCP binding over Auteric's canonical commerce Gateway.

This endpoint accepts the official UCP tool names and wire payloads.  It keeps
merchant mappings, policy, ownership, idempotency and execution in the existing
Gateway instead of creating a second commerce runtime.
"""

import json
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .mcp import PROTOCOL
from .storage import digest, encode
from .ucp import VERSION, encode_response, validate_request


UCP_TO_CANONICAL = {
    "search_catalog": "search_products",
    "lookup_catalog": "lookup_products",
    "get_product": "get_product",
    "create_cart": "create_cart",
    "get_cart": "get_cart",
    "update_cart": "replace_cart_items",
    "cancel_cart": "cancel_cart",
    "create_checkout": "create_checkout",
    "get_checkout": "get_checkout",
    "update_checkout": "update_checkout",
    "complete_checkout": "complete_checkout",
    "cancel_checkout": "cancel_checkout",
    "get_order": "get_order",
}

READ_TOOLS = frozenset({"search_catalog", "lookup_catalog", "get_product", "get_cart", "get_checkout", "get_order"})

META_SCHEMA = {
    "type": "object",
    "description": "UCP request metadata.",
    "required": ["ucp-agent"],
    "additionalProperties": True,
    "properties": {
        "ucp-agent": {
            "type": "object",
            "required": ["profile"],
            "properties": {"profile": {"type": "string", "format": "uri"}},
        },
        "idempotency-key": {"type": "string", "format": "uuid"},
    },
}

TOOL_ARGUMENTS = {
    "search_catalog": {"catalog": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/catalog_search.json#/$defs/search_request"}},
    "lookup_catalog": {"catalog": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/catalog_lookup.json#/$defs/lookup_request"}},
    "get_product": {"catalog": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/catalog_lookup.json#/$defs/get_product_request"}},
    "create_cart": {"cart": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/cart.json"}},
    "get_cart": {"id": {"type": "string"}},
    "update_cart": {"id": {"type": "string"}, "cart": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/cart.json"}},
    "cancel_cart": {"id": {"type": "string"}},
    "create_checkout": {"checkout": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/checkout.json"}},
    "get_checkout": {"id": {"type": "string"}},
    "update_checkout": {"id": {"type": "string"}, "checkout": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/checkout.json"}},
    "complete_checkout": {"id": {"type": "string"}, "checkout": {"$ref": f"https://ucp.dev/{VERSION}/schemas/shopping/checkout.json"}},
    "cancel_checkout": {"id": {"type": "string"}},
    "get_order": {"id": {"type": "string", "description": "The unique identifier of the order."}},
}


def _tool_schema(name):
    properties = {"meta": META_SCHEMA, **TOOL_ARGUMENTS[name]}
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


def _profile(meta, *, development=False):
    if not isinstance(meta, dict) or not isinstance(meta.get("ucp-agent"), dict):
        raise HTTPException(422, "UCP MCP requests require meta.ucp-agent.profile")
    profile = meta["ucp-agent"].get("profile")
    if not isinstance(profile, str) or len(profile) > 2048:
        raise HTTPException(422, "UCP agent profile must be a URL")
    url = urlsplit(profile)
    local = development and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (url.scheme != "https" and not local) or not url.hostname or url.username or url.password or url.fragment:
        raise HTTPException(422, "UCP agent profile must use HTTPS")
    return profile


def _arguments(tool, arguments):
    if tool == "search_catalog":
        return validate_request("search_products", arguments.get("catalog", {}))
    if tool == "lookup_catalog":
        return validate_request("lookup_products", arguments.get("catalog", {}))
    if tool == "get_product":
        return validate_request("get_product", arguments.get("catalog", {}))
    if tool == "create_cart":
        return validate_request("create_cart", arguments.get("cart", {}))
    if tool in {"get_cart", "cancel_cart"}:
        return {"cart_id": arguments.get("id")}
    if tool == "update_cart":
        value = validate_request("replace_cart_items", arguments.get("cart", {}))
        return {**value, "cart_id": arguments.get("id")}
    if tool == "create_checkout":
        value = dict(arguments.get("checkout") or {})
        if arguments.get("cart_id") is not None:
            value["cart_id"] = arguments["cart_id"]
        return validate_request("create_checkout", value)
    if tool in {"get_checkout", "cancel_checkout"}:
        return {"checkout_id": arguments.get("id")}
    if tool in {"update_checkout", "complete_checkout"}:
        value = validate_request(tool, {"checkout": arguments.get("checkout") or {}})
        return {"checkout_id": arguments.get("id"), **value}
    if tool == "get_order":
        return {"order_id": arguments.get("id")}
    raise HTTPException(404, "Unknown UCP tool")


def attach_ucp_mcp(app, dbs, *, development=False):
    state = app.state

    def authority(request, store_id):
        # Store-state refusal comes before credential-kind validation: an
        # invalid store is indistinguishable from a nonexistent one (404/409),
        # never an auth failure that would confirm the store exists.
        state.require_agent_access(store_id)
        credential = state.auth_token(request, "mcp", store_id)
        with dbs.db() as db:
            row = db.execute(
                "SELECT scopes FROM mcp_grants WHERE credential=? AND store=?",
                (credential["hash"], store_id),
            ).fetchone()
        if not row:
            raise HTTPException(401, "UCP MCP grant was revoked")
        return credential, set(json.loads(row["scopes"]))

    def enabled_tools(store_id, scopes):
        active = {op for op in scopes if op in state.inputs and state.operation_enabled(store_id, op)}
        if not state.ucp_configuration(store_id)["payment_handlers"] or "get_order" not in active:
            active.discard("complete_checkout")
        tools = set()
        for tool, operation in UCP_TO_CANONICAL.items():
            if operation in active or (tool == "lookup_catalog" and "get_product" in active):
                tools.add(tool)
        return tools

    def bind_session(store_id, credential, profile):
        session = digest(encode({"kind": "ucp-mcp", "credential": credential["hash"], "profile": profile}))
        with dbs.db() as db:
            row = db.execute(
                "SELECT 1 FROM agent_sessions WHERE hash=? AND store=? AND agent=?",
                (session, store_id, credential["hash"]),
            ).fetchone()
            if row:
                db.execute("UPDATE agent_sessions SET expires=? WHERE hash=?", (time.time() + 3600, session))
            else:
                db.execute(
                    "INSERT INTO agent_sessions VALUES(?,?,?,?)",
                    (session, store_id, credential["hash"], time.time() + 3600),
                )
        return session

    async def sync_discounts(store_id, cart, desired, scopes, credential, agent_session, key):
        if desired is None:
            return cart
        if (
            not isinstance(desired, list)
            or len(desired) > 20
            or any(not isinstance(code, str) or not code.strip() or len(code) > 128 for code in desired)
        ):
            raise HTTPException(422, "discounts.codes must be an array of at most 20 non-empty codes")
        required = {"apply_discount_code", "remove_discount_code"}
        if not required <= scopes or not all(state.operation_enabled(store_id, op) for op in required):
            raise HTTPException(409, "UCP discount fields require active apply/remove discount adapters")
        current = set(cart.get("metadata", {}).get("discount_codes", []))
        target = set(desired)
        for index, code in enumerate(sorted(current - target)):
            envelope = await state.execute_action(
                store_id, "remove_discount_code", {"cart_id": cart["id"], "code": code},
                credential["hash"], agent_session, f"{key}:discount-remove:{index}", protocol="MCP",
            )
            if envelope["state"] != "executed":
                raise HTTPException(409, "Merchant could not remove the requested discount")
            cart = envelope["result"]
        for index, code in enumerate(sorted(target - current)):
            envelope = await state.execute_action(
                store_id, "apply_discount_code", {"cart_id": cart["id"], "code": code},
                credential["hash"], agent_session, f"{key}:discount-apply:{index}", protocol="MCP",
            )
            if envelope["state"] != "executed":
                raise HTTPException(409, "Merchant could not apply the requested discount")
            cart = envelope["result"]
        return cart

    async def execute(store_id, request):
        credential, scopes = authority(request, store_id)
        if not dbs.limit("ucp-mcp:" + credential["hash"], 120):
            raise HTTPException(429, "UCP MCP rate limit exceeded")
        try:
            message = await request.json()
        except ValueError:
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise HTTPException(400, "Expected one JSON-RPC 2.0 message")
        request_id, method = message.get("id"), message.get("method")
        if method == "initialize":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": PROTOCOL,
                 "capabilities": {"tools": {"listChanged": False}},
                 "serverInfo": {"name": "auteric-ucp-shopping", "version": VERSION}}},
                headers={
                    "MCP-Protocol-Version": PROTOCOL,
                    # Streamable HTTP clients retain this opaque identifier for
                    # the following tools/list and tools/call requests.
                    "MCP-Session-Id": secrets.token_urlsafe(24),
                },
            )
        if method == "tools/list":
            tools = sorted(enabled_tools(store_id, scopes))
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": name, "description": f"UCP {VERSION} {name}", "inputSchema": _tool_schema(name)}
                for name in tools
            ]}}
        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"}}
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("arguments"), dict):
            raise HTTPException(422, "UCP tool call requires params.arguments")
        tool, arguments = params.get("name"), params["arguments"]
        if tool not in enabled_tools(store_id, scopes):
            raise HTTPException(403, "UCP tool is outside this Store grant or not active")
        meta = arguments.get("meta")
        profile = _profile(meta, development=development)
        key = meta.get("idempotency-key") if isinstance(meta, dict) else None
        if tool in {"complete_checkout", "cancel_checkout", "cancel_cart"} and (
            not isinstance(key, str) or len(key) > 128
        ):
            raise HTTPException(422, "This UCP mutation requires meta.idempotency-key")
        key = key or digest(encode({"profile": profile, "tool": tool, "arguments": arguments}))
        agent_session = bind_session(store_id, credential, profile)
        operation = UCP_TO_CANONICAL[tool]
        data = _arguments(tool, arguments)
        discount_codes = data.pop("_discount_codes", None)
        if operation == "lookup_products":
            products = []
            for index, product_id in enumerate(data["ids"]):
                envelope = await state.execute_action(
                    store_id, "get_product", {"product_id": product_id}, credential["hash"],
                    agent_session, f"{key}:{index}", protocol="MCP",
                )
                if envelope["state"] != "executed":
                    return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True,
                            "structuredContent": envelope, "content": [{"type": "text", "text": json.dumps(envelope)}]}}
                products.append(envelope["result"])
            structured = encode_response("lookup_products", {"ids": data["ids"], "products": products},
                                         allow_local_http=development)
        elif operation == "create_checkout" and "items" in data:
            if "create_cart" not in scopes or not state.operation_enabled(store_id, "create_cart"):
                raise HTTPException(409, "UCP create_checkout from line_items requires an active create_cart adapter")
            cart = await state.execute_action(
                store_id, "create_cart", {"currency": "USD", "items": data.pop("items")}, credential["hash"],
                agent_session, key + ":cart", protocol="MCP",
            )
            if cart["state"] != "executed":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True,
                        "structuredContent": cart, "content": [{"type": "text", "text": json.dumps(cart)}]}}
            cart_result = await sync_discounts(
                store_id, cart["result"], discount_codes, scopes, credential, agent_session, key
            )
            envelope = await state.execute_action(
                store_id, operation, {**data, "cart_id": cart_result["id"]}, credential["hash"],
                agent_session, key, protocol="MCP",
            )
            if envelope["state"] != "executed":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True,
                        "structuredContent": envelope, "content": [{"type": "text", "text": json.dumps(envelope)}]}}
            result = {"checkout": envelope["result"], "cart": cart_result}
            structured = encode_response(
                operation, result, allow_local_http=development,
                payment_handlers=state.ucp_configuration(store_id)["payment_handlers"],
            )
        else:
            envelope = await state.execute_action(
                store_id, operation, data, credential["hash"], agent_session, key, protocol="MCP"
            )
            if envelope["state"] != "executed":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True,
                        "structuredContent": envelope, "content": [{"type": "text", "text": json.dumps(envelope)}]}}
            result = envelope["result"]
            if operation in {"create_cart", "replace_cart_items"}:
                result = await sync_discounts(
                    store_id, result, discount_codes, scopes, credential, agent_session, key
                )
            if operation in {"create_checkout", "get_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}:
                cart_id = result.get("cart_id")
                if cart_id:
                    cart = await state.execute_action(
                        store_id, "get_cart", {"cart_id": cart_id}, credential["hash"], agent_session,
                        key + ":cart-snapshot", protocol="MCP",
                    )
                    cart_result = await sync_discounts(
                        store_id, cart["result"], discount_codes, scopes, credential, agent_session, key
                    )
                    wire_result = {"checkout": result, "cart": cart_result}
                    if operation == "complete_checkout" and result.get("metadata", {}).get("order_id"):
                        order = await state.execute_action(
                            store_id, "get_order", {"order_id": result["metadata"]["order_id"]},
                            credential["hash"], agent_session, key + ":order-confirmation", protocol="MCP",
                        )
                        if order["state"] != "executed":
                            return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True,
                                    "structuredContent": order,
                                    "content": [{"type": "text", "text": json.dumps(order)}]}}
                        wire_result["order"] = order["result"]
                    result = wire_result
            structured = encode_response(
                operation, result, allow_local_http=development,
                payment_handlers=state.ucp_configuration(store_id)["payment_handlers"],
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "structuredContent": structured,
            "content": [{"type": "text", "text": json.dumps(structured, separators=(",", ":"))}],
            "isError": False,
        }}

    @app.post("/ucp/{store_id}/mcp")
    async def ucp_mcp(store_id: str, request: Request):
        return await execute(store_id, request)

    @app.post("/agent-commerce/{hostname}/mcp")
    async def public_ucp_mcp(hostname: str, request: Request):
        store = state.resolve_public_store(hostname)
        return await execute(store["id"], request)
