"""Developer-facing Installation MCP, separate from the shopper Runtime MCP.

It reads the Kit's canonical registry at runtime and records only dedicated
merchant Auteric endpoints.  It never executes merchant code or exposes a
merchant's existing public route.
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .storage import digest, encode, uid

PROTOCOL = "2025-11-25"
ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "kits/auteric-kit/runtime/sdk/src/auteric_edge/capability_registry.py"


def registry_module():
    spec = importlib.util.spec_from_file_location("auteric_canonical_registry", REGISTRY)
    if not spec or not spec.loader:
        raise RuntimeError("Canonical Auteric capability registry is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def attach_installation_mcp(app, dbs):
    state = app.state
    with dbs.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS installation_implementations(
            id TEXT PRIMARY KEY,store TEXT NOT NULL,operation TEXT NOT NULL,contract_version TEXT NOT NULL,
            endpoint TEXT NOT NULL,method TEXT NOT NULL,adapter TEXT NOT NULL,internal_target TEXT NOT NULL,
            snapshot_digest TEXT,registry_version TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
            UNIQUE(store,operation))""")

    def actor(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if token:
            with dbs.db() as db:
                row = db.execute("SELECT users.* FROM installation_oauth_tokens JOIN users ON users.id=installation_oauth_tokens.user_id WHERE access_hash=? AND expires>? AND revoked IS NULL AND scope=?", (digest(token), time.time(), "installation.mcp")).fetchone()
            if row:
                return dict(row)
        # Dashboard access remains useful for local administration, but remote
        # MCP clients must authenticate with OAuth rather than inherit a session.
        if request.headers.get("x-auteric-console") == "1":
            return state.user_dependency(request)
        raise HTTPException(401, "OAuth bearer token required")

    def owned(store_id, request):
        return state.owned(store_id, actor(request))

    def registry():
        return registry_module()

    def contract(operation):
        module = registry()
        item = module.any_contract(operation)
        return module, item

    def schema(operation):
        model = state.inputs.get(operation)
        return model.model_json_schema() if model else {"type": "object", "description": "Defined for a future engine release."}

    def implementation_rows(store_id):
        with dbs.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM installation_implementations WHERE store=? ORDER BY operation", (store_id,))]

    def status(store_id, request):
        store = owned(store_id, request)
        report = {"store_id": store_id, "environment": store["environment"], "registry_version": registry().registry_document()["schema_version"],
                  "implementations": implementation_rows(store_id), "runtime_enabled_operations": sorted(
                      op for op in state.inputs if state.operation_enabled(store_id, op)),
                  "domain_verified": bool(store["verified"]), "activation": "enabled" if store["agent_access_enabled"] else "disabled"}
        report["blocking_issues"] = []
        if not report["domain_verified"] and store["environment"] == "production": report["blocking_issues"].append("production_domain_unverified")
        if any(row["state"] != "validated" for row in report["implementations"]): report["blocking_issues"].append("implementation_validation_required")
        report["next_actions"] = ["Register dedicated endpoints", "Validate contracts", "Enable from Dashboard after deployment verification"]
        return report

    async def call_tool(name, arguments, request):
        if name == "get_capability_registry":
            document = registry().registry_document()
            mode = arguments.get("status", "all")
            if mode == "runtime_enabled": document["contracts"] = [x for x in document["contracts"] if x["release_stage"] == "current"]
            elif mode == "future": document["contracts"] = [x for x in document["contracts"] if x["release_stage"] == "planned"]
            elif mode != "all": raise HTTPException(422, "status must be all, runtime_enabled, or future")
            return document
        if name == "get_capability_contract":
            module, item = contract(arguments["operation"])
            return {"operation": item.operation, "capability": item.capability, "tool": item.operation, "contract_version": "v1",
                    "path": module.canonical_path(item.operation), "http_methods": list(item.http_methods), "input_schema": schema(item.operation),
                    "activation_requirements": list(item.activation_requirements), "runtime_supported": item.release_stage == "current", "release_stage": item.release_stage}
        if name == "propose_capability_bindings":
            inventory = arguments.get("api_inventory")
            if not isinstance(inventory, list):
                raise HTTPException(422, "api_inventory must be the complete scanner inventory")
            proposals = []
            for candidate in inventory:
                if not isinstance(candidate, dict) or not candidate.get("tool_eligible"):
                    continue
                operation = candidate.get("canonical_candidate")
                if not isinstance(operation, str):
                    continue
                try:
                    module, item = contract(operation)
                except ValueError:
                    continue
                if item.release_stage != "current":
                    continue
                proposals.append({
                    "operation": operation, "capability": item.capability,
                    "tool": item.operation, "contract_version": "v1",
                    "canonical_endpoint": module.canonical_path(operation),
                    "allowed_methods": list(item.http_methods),
                    "merchant_candidate": {key: candidate.get(key) for key in ("method", "route", "source", "line")},
                    "state": "requires_generated_adapter_and_contract_test",
                })
            return {"registry_version": registry().registry_document()["schema_version"], "proposals": proposals,
                    "rule": "This is a read-only plan. Register only after generated adapter, lifecycle tests and Dashboard activation."}
        store_id = arguments["store_id"]
        if name == "get_installation_status": return status(store_id, request)
        if name == "create_integration":
            owned(store_id, request)
            return {"integration_id": store_id, "created": False, "state": "development", "note": "The existing Store record is the integration identity; production domain ownership remains unverified."}
        if name == "register_capability_implementation":
            store = owned(store_id, request)
            module, item = contract(arguments["operation"])
            endpoint, method = arguments["endpoint"], arguments["method"]
            if endpoint != module.canonical_path(item.operation) or not endpoint.startswith("/api/auteric/v1/"):
                raise HTTPException(422, "Auteric must register its exact dedicated canonical endpoint, never a merchant route")
            if method not in item.http_methods: raise HTTPException(422, "HTTP method conflicts with the canonical contract")
            if not arguments["adapter"].startswith("Auteric") or not arguments["internal_target"]:
                raise HTTPException(422, "Use a dedicated Auteric adapter and an explicit internal target")
            now = time.time()
            with dbs.db() as db:
                db.execute("""INSERT INTO installation_implementations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(store,operation) DO UPDATE SET contract_version=excluded.contract_version,endpoint=excluded.endpoint,method=excluded.method,adapter=excluded.adapter,internal_target=excluded.internal_target,snapshot_digest=excluded.snapshot_digest,registry_version=excluded.registry_version,state='registered',updated=excluded.updated""",
                    (uid(), store_id, item.operation, "v1", endpoint, method, arguments["adapter"], arguments["internal_target"], arguments.get("snapshot_digest"), registry().registry_document()["schema_version"], "registered", now, now))
            dbs.event(store["org"], store_id, actor(request)["id"], "installation.implementation.registered", {"operation": item.operation, "endpoint": endpoint})
            return {"operation": item.operation, "state": "registered", "runtime_supported": item.release_stage == "current"}
        if name in {"get_reconciliation_status", "run_reconciliation", "get_reconciliation_changes", "get_required_upgrades"}:
            owned(store_id, request)
            rows = implementation_rows(store_id)
            version = registry().registry_document()["schema_version"]
            stale = [row["operation"] for row in rows if row["registry_version"] != version or row["state"] != "validated"]
            return {"registry_version": version, "state": "review_required" if stale else "no_change", "impacted_operations": stale,
                    "event_contract": registry().registry_document()["code_change_reconciliation"], "remote_webhook": "pending_integration"}
        if name in {"validate_capability", "run_contract_tests", "run_security_checks", "validate_integration"}:
            owned(store_id, request)
            rows = implementation_rows(store_id)
            target = arguments.get("operation")
            selected = [row for row in rows if not target or row["operation"] == target]
            failures = []
            for row in selected:
                module, item = contract(row["operation"])
                if row["endpoint"] != module.canonical_path(item.operation): failures.append({"operation": item.operation, "code": "CONTRACT_DRIFT"})
                if row["method"] not in item.http_methods: failures.append({"operation": item.operation, "code": "CONTRACT_DRIFT"})
                if not row["adapter"].startswith("Auteric") or not row["internal_target"]: failures.append({"operation": item.operation, "code": "INTERNAL_BINDING_DRIFT"})
            if not failures:
                with dbs.db() as db:
                    db.execute("UPDATE installation_implementations SET state='validated',updated=? WHERE store=?" + (" AND operation=?" if target else ""), (time.time(), store_id, target) if target else (time.time(), store_id))
            return {"passed": not failures, "failures": failures, "checked_operations": [row["operation"] for row in selected], "activation": "Dashboard controls runtime enablement"}
        raise HTTPException(404, "Unknown Installation MCP tool")

    @app.post("/installation-mcp")
    async def installation_mcp(request: Request):
        try:
            actor(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            base = state.installation_mcp_public_url.rstrip("/")
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
        try: message = await request.json()
        except ValueError: raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0": raise HTTPException(400, "Expected JSON-RPC 2.0")
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "auteric-installation", "version": "0.1.0"}}}, headers={"MCP-Session-Id": secrets.token_urlsafe(24)})
        if method == "tools/list":
            names = ["get_installation_status", "create_integration", "get_capability_registry", "get_capability_contract", "propose_capability_bindings", "register_capability_implementation", "get_reconciliation_status", "run_reconciliation", "get_reconciliation_changes", "get_required_upgrades", "validate_capability", "run_contract_tests", "run_security_checks", "validate_integration"]
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}}
        if method == "tools/call":
            params = message.get("params", {})
            if not isinstance(params, dict) or not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict): raise HTTPException(422, "Invalid tool parameters")
            result = await call_tool(params["name"], params.get("arguments", {}), request)
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}], "isError": False}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
