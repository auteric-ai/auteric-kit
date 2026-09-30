"""One MCP ingress, partitioned by Store and explicit per-client operation grants.

The transport uses JSON responses and the existing deterministic Gateway for calls.
No tool is exposed until its mapping is active, enabled and the Store is ready.
"""

import json
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .product import BY_OPERATION, CAPABILITIES, protocol_input_schema
from .storage import digest, encode

PROTOCOL = "2025-11-25"
READ_OPERATIONS = frozenset(c.operation for c in CAPABILITIES if c.side_effect == "read")


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[str] = Field(default_factory=lambda: sorted(READ_OPERATIONS), min_length=1, max_length=25)


def attach_mcp(app, dbs):
    state = app.state

    def grant_for(request, store_id):
        origin = request.headers.get("origin")
        if origin and origin not in {state.public_url, state.mcp_public_url}:
            raise HTTPException(403, "MCP Origin is not allowed")
        credential = state.auth_token(request, "mcp", store_id)
        with dbs.db() as db:
            row = db.execute("SELECT scopes FROM mcp_grants WHERE credential=? AND store=?",
                             (credential["hash"], store_id)).fetchone()
        if not row:
            raise HTTPException(401, "MCP grant was revoked")
        return credential, set(json.loads(row["scopes"]))

    def session_for(request, store_id, credential):
        session = request.headers.get("mcp-session-id", "")
        if not session:
            raise HTTPException(400, "MCP-Session-Id is required after initialization")
        with dbs.db() as db:
            row = db.execute("SELECT hash FROM agent_sessions WHERE hash=? AND store=? AND agent=? AND expires>?",
                             (digest(session), store_id, credential["hash"], time.time())).fetchone()
        if not row:
            raise HTTPException(404, "MCP session expired or belongs to another Store")
        return row["hash"]

    def available(store_id, scopes):
        state.require_agent_access(store_id)
        return {
            operation
            for operation in scopes
            if operation in BY_OPERATION
            and state.operation_enabled(store_id, operation)
            and state.capability_controls_allowed(store_id, operation)
        }

    @app.post("/api/commerce/stores/{store_id}/mcp-credentials")
    def issue(store_id: str, body: GrantRequest, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        scopes = set(body.operations)
        if len(scopes) != len(body.operations) or not scopes <= BY_OPERATION.keys():
            raise HTTPException(422, "Select unique canonical operations")
        token = secrets.token_urlsafe(40)
        credential_id = digest(token)
        with dbs.db() as db:
            db.execute("INSERT INTO credentials(hash,store,kind,created,revoked) VALUES(?,?,?,?,NULL)",
                       (credential_id, store_id, "mcp", time.time()))
            db.execute("INSERT INTO mcp_grants(credential,store,scopes,created) VALUES(?,?,?,?)",
                       (credential_id, store_id, encode(sorted(scopes)), time.time()))
        dbs.event(actor["org"], store_id, actor["id"], "mcp.credential.created",
                  {"credential_id": credential_id, "operations": sorted(scopes)})
        return {"token": token, "credential_id": credential_id, "store_id": store_id,
                "operations": sorted(scopes), "mcp_url": state.mcp_public_url + "/mcp/" + store_id,
                "display_once": True}

    @app.get("/api/commerce/stores/{store_id}/mcp-credentials")
    def list_grants(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            rows = db.execute("SELECT g.credential,g.scopes,g.created,c.revoked FROM mcp_grants g "
                              "JOIN credentials c ON c.hash=g.credential WHERE g.store=? ORDER BY g.created DESC",
                              (store_id,)).fetchall()
        return {"mcp_url": state.mcp_public_url + "/mcp/" + store_id,
                "credentials": [{"credential_id": r["credential"], "operations": json.loads(r["scopes"]),
                                 "created": r["created"], "revoked": bool(r["revoked"])} for r in rows]}

    @app.delete("/api/commerce/stores/{store_id}/mcp-credentials/{credential_id}")
    def revoke(store_id: str, credential_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            changed = db.execute("UPDATE credentials SET revoked=? WHERE hash=? AND store=? AND kind='mcp' AND revoked IS NULL",
                                 (time.time(), credential_id, store_id)).rowcount
            if changed:
                db.execute("DELETE FROM agent_sessions WHERE store=? AND agent=?", (store_id, credential_id))
        if not changed:
            raise HTTPException(404, "Active MCP credential not found")
        dbs.event(actor["org"], store_id, actor["id"], "mcp.credential.revoked", {"credential_id": credential_id})
        return {"revoked": True}

    @app.post("/mcp/{store_id}")
    async def mcp(store_id: str, request: Request):
        credential, scopes = grant_for(request, store_id)
        if not dbs.limit("mcp:" + credential["hash"], 120):
            raise HTTPException(429, "MCP rate limit exceeded")
        try:
            message = await request.json()
        except ValueError:
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise HTTPException(400, "Expected one JSON-RPC 2.0 message")
        method = message.get("method")
        request_id = message.get("id")
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            raise HTTPException(400, "JSON-RPC ID must be a string or integer")
        if "params" in message and not isinstance(message["params"], dict):
            raise HTTPException(400, "MCP params must be an object")
        if not isinstance(method, str):
            raise HTTPException(400, "JSON-RPC method is required")
        if method == "initialize":
            params = message.get("params")
            if request_id is None or not isinstance(params, dict):
                raise HTTPException(400, "Initialize requires an ID and params object")
            client_version = params.get("protocolVersion")
            if client_version != PROTOCOL:
                raise HTTPException(400, "Unsupported MCP protocol version")
            session = secrets.token_urlsafe(32)
            with dbs.db() as db:
                db.execute("INSERT INTO agent_sessions VALUES(?,?,?,?)",
                           (digest(session), store_id, credential["hash"], time.time() + 3600))
            return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                                 "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                                            "serverInfo": {"name": "auteric-commerce", "version": "0.1.0"}}},
                                headers={"MCP-Session-Id": session})
        if request.headers.get("mcp-protocol-version") != PROTOCOL:
            raise HTTPException(400, "MCP-Protocol-Version must match initialization")
        session_id = session_for(request, store_id, credential)
        if request_id is None:
            if method == "notifications/initialized":
                return Response(status_code=202)
            raise HTTPException(400, "Unsupported MCP notification")
        if method == "ping":
            result = {}
        elif method == "tools/list":
            enabled = available(store_id, scopes)
            tools = [{"name": c.operation, "title": c.name, "description": c.description,
                      "inputSchema": protocol_input_schema(state.inputs[c.operation]),
                      "annotations": {"readOnlyHint": c.side_effect == "read"}}
                     for c in CAPABILITIES if c.operation in enabled]
            result = {"tools": tools}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict):
                raise HTTPException(400, "Tool call params must be an object")
            operation = params.get("name")
            if not isinstance(operation, str) or operation not in available(store_id, scopes):
                raise HTTPException(403, "Tool is outside this Store grant or not active")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise HTTPException(422, "Tool arguments must be an object")
            key = request.headers.get("idempotency-key")
            if operation not in READ_OPERATIONS and (not key or len(key) > 128):
                raise HTTPException(400, "Write tools require an Idempotency-Key header")
            if not key:
                key = secrets.token_urlsafe(16)
            envelope = await state.execute_action(store_id, operation, arguments,
                                                   credential["hash"], session_id, key, protocol="MCP")
            result = {"content": [{"type": "text", "text": json.dumps(envelope, separators=(",", ":"))}],
                      "isError": envelope.get("state") not in {"executed", "approved"}}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32601, "message": "Method not found"}})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @app.get("/mcp/{store_id}")
    def mcp_get(store_id: str, request: Request):
        grant_for(request, store_id)
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @app.delete("/mcp/{store_id}")
    def mcp_delete(store_id: str, request: Request):
        credential, _ = grant_for(request, store_id)
        session_for(request, store_id, credential)
        with dbs.db() as db:
            db.execute("DELETE FROM agent_sessions WHERE hash=? AND store=? AND agent=?",
                       (digest(request.headers["mcp-session-id"]), store_id, credential["hash"]))
        return Response(status_code=204)
