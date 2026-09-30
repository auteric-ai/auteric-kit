import hmac
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .connectors import Shopify
from .demo import Demo
from .domain import Approval, DomainError, PriceProposal, Settings
from .ledger import Ledger


def create_app(settings: Settings, connector=None):
    app = FastAPI(title="Auteric Commerce Security Runtime", version="0.2.0")
    connector = connector or (Demo(settings.database + ".catalog") if settings.mode == "demo" else Shopify(settings))
    ledger = Ledger(settings, connector)
    app.state.ledger = ledger

    def require(*roles):
        def authenticate(authorization: str = Header(default="")):
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            principal = next((p for p in settings.principals if hmac.compare_digest(token.encode(), p.token.get_secret_value().encode())), None)
            if principal is None:
                raise HTTPException(401, "Valid bearer credential required")
            if principal.role not in roles:
                raise HTTPException(403, "Role not authorized for this operation")
            return principal
        return authenticate

    def legacy_write():
        if not settings.legacy_gateway_enabled:
            raise HTTPException(410, "Legacy mutation routes disabled. Use /v1/actions/evaluate.")

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": settings.mode, "live_writes": settings.mode == "shopify" and settings.live_writes}

    @app.get("/v1/capabilities")
    def capabilities(actor=Depends(require("agent", "approver", "executor"))):
        if not settings.legacy_gateway_enabled:
            return {"merchant_id": settings.merchant_id,
                    "supported": ["read_variant", "evaluate_price_change", "bulk_evaluation", "approve_exact_action", "execute_single_price_change", "reject", "audit"],
                    "unsupported": ["bulk_execution", "checkout", "refunds", "inventory_writes", "oauth_install", "analytics"],
                    "policy": app.state.runtime.policy.model_dump(mode="json"), "legacy_gateway_enabled": False}
        return {"merchant_id": settings.merchant_id, "supported": ["read_variant", "stage_single_variant_price", "approve", "execute", "discard", "audit"], "unsupported": ["checkout", "refunds", "campaigns", "scheduled_promotions", "inventory_writes", "oauth_install", "analytics"], "max_delta_percent": str(settings.max_delta_percent)}

    @app.get("/v1/variant")
    def variant(id: str = Query(max_length=160), actor=Depends(require("agent", "approver", "executor"))):
        return connector.get(id)

    @app.post("/v1/changes", status_code=201)
    def stage(body: PriceProposal, actor=Depends(require("agent"))):
        legacy_write()
        return ledger.stage(body, actor.subject)

    @app.get("/v1/changes")
    def changes(actor=Depends(require("agent", "approver", "executor"))):
        return ledger.list()

    @app.get("/v1/changes/{change_id}")
    def change(change_id: str, actor=Depends(require("agent", "approver", "executor"))):
        return ledger.get(change_id)

    @app.post("/v1/changes/{change_id}/approve")
    def approve(change_id: str, body: Approval, actor=Depends(require("approver"))):
        legacy_write()
        return ledger.approve(change_id, body.digest, actor.subject)

    @app.post("/v1/changes/{change_id}/execute")
    def execute(change_id: str, actor=Depends(require("agent", "executor"))):
        legacy_write()
        return ledger.execute(change_id, actor.subject)

    @app.post("/v1/changes/{change_id}/discard")
    def discard(change_id: str, actor=Depends(require("approver"))):
        legacy_write()
        return ledger.discard(change_id, actor.subject)

    @app.get("/v1/audit")
    def audit(actor=Depends(require("approver"))):
        return ledger.audit()

    from .runtime_api import attach_runtime
    attach_runtime(app, settings, connector)
    return app


def factory():
    # No insecure default credentials. Fail startup if configuration is absent.
    path = Path(os.environ.get("AUTERIC_CONFIG", "config.json"))
    return create_app(Settings.model_validate_json(path.read_text()))
