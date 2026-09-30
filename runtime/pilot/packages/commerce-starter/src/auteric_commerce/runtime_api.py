"""Authenticated transport for the independent commerce runtime.

The optional loopback demo session is NOT authentication for a hosted service.
"""
import hmac
import ipaddress
import secrets
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .actions import CommerceAction
from .domain import Approval
from .policy import PolicyConfig
from .runtime import Runtime


def attach_runtime(app, settings, connector, *, existing_runtime=None):
    policy = existing_runtime.policy if existing_runtime is not None else (PolicyConfig.model_validate_json(Path(settings.runtime_policy_path).read_text()) if settings.runtime_policy_path else PolicyConfig())
    runtime = existing_runtime or Runtime(database=settings.database + ".runtime", merchant_id=settings.merchant_id,
                      backend=connector, policy=policy, ttl_seconds=settings.proposal_ttl_seconds,
                      binding={"mode": settings.mode, "shop_domain": settings.shop_domain,
                               "api_version": settings.api_version},
                      live_writes=settings.mode == "demo" or settings.live_writes)
    app.state.runtime = runtime
    # Single-process developer sessions only, short-lived and never in URL/storage.
    sessions = {}

    def local_request(request):
        try:
            peer = ipaddress.ip_address(request.client.host)
            loopback = peer.is_loopback
        except (ValueError, AttributeError):
            loopback = False
        return (settings.local_dev and settings.mode == "demo" and loopback
                and request.url.hostname in {"localhost", "127.0.0.1", "::1"})

    def check_origin(request):
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.headers.get("origin") not in {None, expected}:
            raise HTTPException(403, "Cross-origin console requests are refused")

    def require(*roles):
        def authenticate(request: Request):
            header = request.headers.get("authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            principal = next((p for p in settings.principals if token and hmac.compare_digest(
                token.encode(), p.token.get_secret_value().encode())), None)
            if principal is None and local_request(request):
                session = request.cookies.get("auteric_dev", "")
                if sessions.get(session, 0) > time.time():
                    if request.method not in {"GET", "HEAD"}:
                        check_origin(request)
                        if request.headers.get("x-auteric-console") != "1":
                            raise HTTPException(403, "Console request header required")
                    principal = next(p for p in settings.principals if p.role == "approver")
            if principal is None:
                raise HTTPException(401, "Valid bearer credential or explicit local demo session required")
            if principal.role not in roles:
                raise HTTPException(403, "Role not authorized for this operation")
            return principal
        return authenticate

    def bound_action(action, actor):
        if action.merchant_id != settings.merchant_id:
            raise HTTPException(403, "Action merchant differs from authenticated merchant")
        data = action.model_dump(mode="json")
        data.update(principal={"subject": actor.human_subject, "roles": actor.business_roles},
                    agent={"id": actor.agent_id or actor.subject}, app_id=actor.subject,
                    delegation=None if actor.delegation_scopes is None else {
                        "scopes": actor.delegation_scopes, "expires_at": actor.delegation_expires_at})
        return CommerceAction.model_validate(data)

    def owned(action_id, actor):
        row = runtime.get_action(action_id)
        if actor.role == "agent" and row["action"].get("app_id") != actor.subject:
            raise HTTPException(403, "Action belongs to a different application credential")
        return row

    def current_authority(row):
        source = next((p for p in settings.principals if p.role == "agent" and p.subject == row["action"].get("app_id")), None)
        if source is None:
            raise HTTPException(403, "Originating application authority was revoked; evaluate a new action")
        bound = bound_action(CommerceAction.model_validate(row["action"]), source).model_dump(mode="json")
        if any(bound[key] != row["action"][key] for key in ("principal", "agent", "app_id", "delegation")):
            raise HTTPException(403, "Principal or delegated authority changed; evaluate a new action")

    @app.post("/v1/actions/evaluate")
    async def evaluate(action: CommerceAction, actor=Depends(require("agent"))):
        if existing_runtime is not None:
            raise HTTPException(409, "Approval-only host: the original protected backend owns proposal and execution")
        if settings.legacy_gateway_enabled:
            raise HTTPException(409, "Disable legacy gateway routes before enabling runtime writes")
        return await runtime.evaluate(bound_action(action, actor))

    @app.get("/v1/actions")
    def list_actions(state: str | None = None, limit: int = Query(100, ge=1, le=100), actor=Depends(require("agent", "approver", "executor"))):
        rows = runtime.list_actions(state=state, limit=limit)
        return [r for r in rows if actor.role != "agent" or r["action"].get("app_id") == actor.subject]

    @app.get("/v1/actions/{action_id}")
    def get_action(action_id: str, actor=Depends(require("agent", "approver", "executor"))):
        return owned(action_id, actor)

    @app.post("/v1/actions/{action_id}/approve")
    def approve_action(action_id: str, body: Approval, actor=Depends(require("approver"))):
        return runtime.approve(action_id, body.digest, actor.human_subject or actor.subject)

    @app.post("/v1/actions/{action_id}/reject")
    def reject_action(action_id: str, actor=Depends(require("approver"))):
        return runtime.reject(action_id, actor.human_subject or actor.subject)

    @app.post("/v1/actions/{action_id}/execute")
    async def execute_action(action_id: str, actor=Depends(require("agent", "executor"))):
        if existing_runtime is not None:
            raise HTTPException(409, "Approval-only host: execute through the original protected backend")
        if settings.legacy_gateway_enabled:
            raise HTTPException(409, "Disable legacy gateway routes before enabling runtime writes")
        current_authority(owned(action_id, actor))
        return await runtime.execute(action_id, actor.subject)

    @app.get("/v1/runtime/audit")
    def audit(action_id: str | None = None, limit: int = Query(200, ge=1, le=200), actor=Depends(require("approver"))):
        return runtime.audit(action_id=action_id, limit=limit)

    @app.get("/v1/runtime/policy")
    def current_policy(actor=Depends(require("agent", "approver", "executor"))):
        return {"merchant_id": settings.merchant_id, "policy": policy.model_dump(mode="json"),
                "execution": "single-resource price.update only", "legacy_gateway_enabled": settings.legacy_gateway_enabled}

    @app.get("/console/config")
    def console_config(request: Request):
        return {"local_demo": local_request(request), "mode": settings.mode}

    @app.post("/dev/session")
    def dev_session(request: Request):
        if not local_request(request):
            raise HTTPException(404, "Local demo only")
        check_origin(request)
        if request.headers.get("x-auteric-console") != "1":
            raise HTTPException(403, "Console request header required")
        for key in list(sessions):
            if sessions[key] <= time.time():
                del sessions[key]
        if len(sessions) >= 32:
            raise HTTPException(429, "Too many active demo sessions")
        token = secrets.token_urlsafe(32)
        sessions[token] = time.time() + 3600
        response = JSONResponse({"mode": "local-demo", "role": "approver"})
        response.set_cookie("auteric_dev", token, httponly=True, samesite="strict", max_age=3600,
                            secure=request.url.scheme == "https")
        return response

    @app.delete("/dev/session")
    def end_session(request: Request):
        check_origin(request)
        if request.headers.get("x-auteric-console") != "1":
            raise HTTPException(403, "Console request header required")
        sessions.pop(request.cookies.get("auteric_dev", ""), None)
        response = Response(status_code=204)
        response.delete_cookie("auteric_dev")
        return response

    class Scenario(BaseModel):
        new_price: str = Field(pattern=r"^[0-9]{1,9}(\.[0-9]{1,2})?$")

    @app.post("/dev/scenarios")
    async def scenario(body: Scenario, request: Request, actor=Depends(require("approver"))):
        if not local_request(request):
            raise HTTPException(404, "Local demo only")
        agent = next(p for p in settings.principals if p.role == "agent")
        action = CommerceAction.model_validate({"merchant_id": settings.merchant_id, "type": "price.update",
                                               "source": "local-demo", "protocol": "REST",
                                               "items": [{"resource_id": "summer-shoes", "proposed_after": body.new_price}],
                                               "reason": "Synthetic Summer Shoes demonstration"})
        return await runtime.evaluate(bound_action(action, agent))

    @app.post("/dev/actions/{action_id}/execute")
    async def demo_execute(action_id: str, request: Request, actor=Depends(require("approver"))):
        if not local_request(request):
            raise HTTPException(404, "Local demo only")
        executor = next(p for p in settings.principals if p.role == "executor")
        current_authority(runtime.get_action(action_id))
        return await runtime.execute(action_id, executor.subject)

    static = Path(__file__).parent / "static"
    app.mount("/console/assets", StaticFiles(directory=static), name="console-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/console", include_in_schema=False)
    def console():
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def browser_safety(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path in {"/", "/console"} or request.url.path.startswith("/console/assets"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response


def create_approval_app(runtime, settings):
    """Serve the console/API for an embedded backend's actual shared Runtime.

    Human credentials must be supplied by the trusted host. This app cannot
    replace the original backend or execute agent-provided callbacks.
    """
    from fastapi import FastAPI
    from .domain import DomainError
    if settings.merchant_id != runtime.merchant_id or settings.local_dev or settings.legacy_gateway_enabled:
        raise ValueError("Approval host requires matching merchant, local_dev=false and legacy routes disabled")
    app = FastAPI(title="Auteric embedded runtime approval host", version="0.2.0")

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    attach_runtime(app, settings, None, existing_runtime=runtime)
    return app
