"""Deployable process wrapper for :mod:`auteric_merchant.sidecar`.

The process reads one immutable public configuration document and resolves
secret references only at execution time.  It exposes MEP/1 plus separate
liveness/readiness endpoints; loading invalid or stale evidence fails startup.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Mapping
from fastapi import Request

from . import contracts_generated as contracts
from .auth import Installation, OperationBinding, TrustBundle
from .fastapi_driver import create_auteric_router
from .sidecar import MerchantIntegrationManifest, MerchantSidecar
from .execution_store import SQLiteExecutionStore
from .sidecar_ops import LocalPolicySnapshot, SQLiteAuditSink, policy_fingerprint

SIDECAR_SCHEMA = "auteric-sidecar/v1"


def _public_key(value: str) -> bytes:
    if value.startswith("hex:"):
        raw = bytes.fromhex(value[4:])
    else:
        encoded = value[7:] if value.startswith("base64:") else value
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(raw) != 32:
        raise ValueError("pinned Ed25519 public keys must be exactly 32 bytes")
    return raw


def _secret_resolver(reference: str) -> str:
    if reference.startswith("env:"):
        name = reference[4:]
        if not name or name not in os.environ:
            raise RuntimeError(f"required environment secret {name or '<empty>'} is unavailable")
        return os.environ[name]
    if reference.startswith("file:"):
        path = Path(reference[5:])
        if not path.is_absolute():
            raise RuntimeError("secret file references must be absolute")
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    raise RuntimeError("secret references must use env:NAME or file:/absolute/path")


def load_sidecar(path: str | os.PathLike[str]) -> MerchantSidecar:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema") != SIDECAR_SCHEMA:
        raise ValueError("sidecar configuration schema is missing or unsupported")
    if not {"schema", "installation", "trust", "integration"}.issubset(raw) or set(raw) - {"schema", "installation", "trust", "integration", "operational"}:
        raise ValueError("sidecar configuration contains unknown or missing top-level fields")
    installation_raw = dict(raw["installation"])
    manifest_raw = installation_raw.pop("manifest")
    # Gateway health probes are authenticated with the same pinned execution
    # JWT as commerce calls, but are deliberately outside the locked commerce
    # registry.  Install the reserved binding locally; it is never advertised
    # as a merchant capability or profile.
    manifest_raw = {
        **dict(manifest_raw),
        "health": {
            "method": "GET",
            "path": "/api/auteric/v1/health",
            "contract_version": contracts.REGISTRY_VERSION,
            "binding_digest": "sha256:" + "0" * 64,
        },
    }
    installation = Installation(
        **installation_raw,
        manifest={name: OperationBinding(**binding) for name, binding in dict(manifest_raw).items()},
    )
    trust_raw = dict(raw["trust"])
    if set(trust_raw) != {"issuer_allowlist", "keys"}:
        raise ValueError("trust bundle contains unknown or missing fields")
    trust = TrustBundle(
        issuer_allowlist=tuple(trust_raw["issuer_allowlist"]),
        keys={kid: _public_key(value) for kid, value in dict(trust_raw["keys"]).items()},
    )
    integration = MerchantIntegrationManifest.from_dict(raw["integration"])
    integration.validate(installation)
    operational = dict(raw.get("operational", {}))
    allowed_operational = {"execution_store", "audit_store", "policy", "control_token_ref", "health_reporting", "storage_mode", "database_secret_ref", "agent_ingress"}
    if set(operational) - allowed_operational:
        raise ValueError("sidecar operational configuration contains unknown fields")
    if not operational.get("policy"):
        raise ValueError("deployable sidecar requires a local policy snapshot")
    execution_store = None
    audit_sink = None
    durable = False
    if operational.get("execution_store"):
        prefix = "sqlite:"
        if operational["execution_store"] == "postgres":
            from .postgres_store import PostgresExecutionStore
            execution_store = PostgresExecutionStore(_secret_resolver(operational["database_secret_ref"]))
        elif not str(operational["execution_store"]).startswith(prefix):
            raise ValueError("the packaged sidecar supports sqlite: execution stores")
        else:
            execution_store = SQLiteExecutionStore(str(operational["execution_store"])[len(prefix):])
    if operational.get("audit_store"):
        prefix = "sqlite:"
        if operational["audit_store"] == "postgres":
            from .postgres_store import PostgresAuditSink
            audit_sink = PostgresAuditSink(_secret_resolver(operational["database_secret_ref"]))
        elif not str(operational["audit_store"]).startswith(prefix):
            raise ValueError("the packaged sidecar supports sqlite: audit stores")
        else:
            audit_sink = SQLiteAuditSink(str(operational["audit_store"])[len(prefix):])
    storage_mode = operational.get("storage_mode", "durable")
    if storage_mode not in {"durable", "ephemeral"}:
        raise ValueError("sidecar storage_mode must be durable or ephemeral")
    durable = execution_store is not None and audit_sink is not None and storage_mode == "durable"
    policy = None
    if operational.get("policy"):
        policy_raw = dict(operational["policy"])
        fingerprint = policy_raw.pop("fingerprint", "")
        if fingerprint != policy_fingerprint(policy_raw):
            raise ValueError("local policy snapshot fingerprint mismatch")
        policy = LocalPolicySnapshot(fingerprint=fingerprint, **policy_raw)
    return MerchantSidecar(
        installation=installation,
        trust=trust,
        profiles=integration.profiles,
        secret_resolver=_secret_resolver,
        execution_store=execution_store,
        audit_sink=audit_sink,
        policy_snapshot=policy,
        durable=durable,
        storage_mode=storage_mode,
    )


def create_sidecar_app(config_path: str | os.PathLike[str] | None = None):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    selected = str(config_path or os.environ.get("AUTERIC_SIDECAR_CONFIG", ""))
    if not selected:
        raise RuntimeError("AUTERIC_SIDECAR_CONFIG is required")
    sidecar = load_sidecar(selected)
    raw_config = json.loads(Path(selected).read_text(encoding="utf-8"))
    operational = dict(raw_config.get("operational", {}))
    control_token_ref = operational.get("control_token_ref")
    reporting = operational.get("health_reporting")
    app = FastAPI(title="Auteric Merchant Sidecar", docs_url=None, redoc_url=None)
    app.state.auteric_sidecar = sidecar
    app.include_router(create_auteric_router(sidecar.runtime))

    if operational.get("agent_ingress"):
        from .gateway_client import SidecarGatewayClient
        ingress=SidecarGatewayClient(sidecar,**operational["agent_ingress"])

        @app.post("/api/auteric/agent/v1/actions/{operation}", include_in_schema=False)
        async def agent_action(operation: str, request: Request):
            size=0
            chunks=[]
            async for chunk in request.stream():
                size+=len(chunk)
                if size>64*1024:
                    return JSONResponse({"error":{"code":"INVALID_INPUT","message":"Request too large"}},status_code=413)
                chunks.append(chunk)
            try:
                data=json.loads(b''.join(chunks))
                if not isinstance(data,dict):raise ValueError()
            except (ValueError,UnicodeDecodeError):
                return JSONResponse({"error":{"code":"INVALID_INPUT","message":"JSON object required"}},status_code=400)
            status,body=await ingress.execute(operation,data,authorization=request.headers.get('authorization',''),
                session=request.headers.get('x-auteric-session',''),idempotency_key=request.headers.get('idempotency-key',''),
                verification=request.headers.get('x-auteric-verification',''))
            return JSONResponse(body,status_code=status)

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"status": "live", "merchant_protocol": "1"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready():
        health = sidecar.health()
        storage_healthy = True
        try:
            for dependency in (sidecar.execution_store, sidecar.audit_sink):
                probe = getattr(dependency, 'check_ready', None)
                if probe:
                    await asyncio.wait_for(probe(), timeout=2)
        except Exception:
            storage_healthy = False
        enabled = health["enabled_operations"]
        policy_valid = health["policy"] is not None and health["policy"]["valid"] and health["policy"]["covers_enabled_operations"]
        writes_enabled = any(__import__("auteric_merchant.contracts_generated", fromlist=["OPERATIONS"]).OPERATIONS[op]["side_effect"] == "write" for op in enabled)
        # Ephemeral storage is an explicitly constrained single-task MVP.
        # It is ready for preview validation but reports its limitation in the
        # response and is never indistinguishable from durable production IO.
        ephemeral_mvp = health["storage_mode"] == "ephemeral"
        status = 200 if storage_healthy and enabled and policy_valid and (health["durable"] or ephemeral_mvp or not writes_enabled) else 503
        return JSONResponse({"status": "ready" if status == 200 else "not_ready", "storage_healthy": storage_healthy, **health}, status_code=status)

    @app.post("/internal/reconcile/{action_id}", include_in_schema=False)
    async def reconcile(action_id: str, request: Request):
        if not control_token_ref:
            return JSONResponse({"error": "disabled"}, status_code=404)
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + _secret_resolver(control_token_ref)
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await sidecar.reconcile(action_id)
        except ValueError:
            return JSONResponse({"error": "not_reconcilable"}, status_code=409)

    if reporting:
        @app.on_event("startup")
        async def start_reporting():
            interval = int(reporting.get("interval_seconds", 60))
            if not 15 <= interval <= 300:
                raise RuntimeError("health reporting interval must be between 15 and 300 seconds")
            endpoint = reporting.get("endpoint", "")
            if not endpoint.startswith("https://"):
                raise RuntimeError("health reporting endpoint must use HTTPS")

            async def report_loop():
                import httpx
                while True:
                    body = json.dumps(sidecar.health(), sort_keys=True, separators=(",", ":")).encode()
                    timestamp = str(int(__import__("time").time()))
                    secret = _secret_resolver(reporting["hmac_secret_ref"]).encode()
                    signature = hmac.new(secret, timestamp.encode() + b"\n" + hashlib.sha256(body).hexdigest().encode(), hashlib.sha256).hexdigest()
                    try:
                        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                            await client.post(endpoint, content=body, headers={
                                "content-type": "application/json", "x-auteric-timestamp": timestamp,
                                "x-auteric-health-signature": signature,
                            })
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(interval)
            app.state.health_reporter = asyncio.create_task(report_loop())

        @app.on_event("shutdown")
        async def stop_reporting():
            task = getattr(app.state, "health_reporter", None)
            if task:
                task.cancel()

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auteric-sidecar")
    parser.add_argument("--config", default=os.environ.get("AUTERIC_SIDECAR_CONFIG"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--check", action="store_true", help="validate configuration and exit without opening a socket")
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config or AUTERIC_SIDECAR_CONFIG is required")
    if args.check:
        sidecar = load_sidecar(args.config)
        print(json.dumps({"status": "valid", **sidecar.health()}, sort_keys=True))
        return 0
    import uvicorn
    uvicorn.run(create_sidecar_app(args.config), host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
