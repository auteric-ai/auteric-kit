"""Server-side effective capability policy (P1-16; plan §6, §14.4, §14.5).

For every canonical operation the effective state is computed from durable
evidence, never from a UI toggle:

    callable = installed ∩ deployed ∩ tested ∩ compatible_contract
               ∩ merchant_enabled ∩ active_installation

- ``GET .../capabilities/effective`` lists detected/installed/tested/deployed/
  enabled plus the single blocking reason per operation (dashboard semantics).
- Enable runs a server-side preflight of every prerequisite; an operation that
  is not deployed, not tested, or bound to an incompatible contract digest can
  NEVER be enabled through the API. Disable commits immediately.
- Per-call enforcement lives in ``app.dispatch`` (checked on every call,
  including inside an open MCP session); tools/list filtering is only a
  display convenience, not the enforcement mechanism.
- In-flight semantics (plan §14.5): a call already dispatched when a disable
  commits may run to completion; issuance of new executions stops at the
  dispatch check. Execution JWTs issued just before the disable expire within
  the MEP/1 window (≤30s + ≤5s skew); disable never cancels an already
  committed merchant transaction.
"""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException

from .installations import get_installation, set_capability_policy
from .product import BY_OPERATION
from .transports.native_http import contracts

# Ordered prerequisite chain; the first failure is the reported reason.
REASONS = (
    "unsupported_operation",
    "installation_missing",
    "installation_revoked",
    "not_installed",
    "not_deployed",
    "tests_not_passed",
    "contract_digest_mismatch",
    "disabled_by_merchant",
)


def _tests_passed(test_evidence) -> bool:
    if not test_evidence:
        return False
    try:
        evidence = json.loads(test_evidence)
    except ValueError:
        return False
    return not (isinstance(evidence, dict) and evidence.get("status", "pass") != "pass")


def operation_states(dbs, store, operation: str) -> dict:
    """Evidence states + blocking reason for one operation on one store."""
    view = {
        "operation": operation,
        "detected": False,
        "installed": False,
        "tested": False,
        "deployed": False,
        "enabled": False,
        "reason": None,
        "contract_digest": None,
        "binding_digest": None,
    }
    if operation not in BY_OPERATION:
        return {**view, "reason": "unsupported_operation"}
    installation = get_installation(dbs, store["id"], store["environment"])
    if not installation:
        return {**view, "reason": "installation_missing"}
    if installation["revoked_at"]:
        return {**view, "reason": "installation_revoked"}
    with dbs.db() as db:
        row = db.execute(
            "SELECT * FROM installation_operations WHERE installation_id=? AND operation=?",
            (installation["id"], operation),
        ).fetchone()
        policy = db.execute(
            "SELECT enabled,revision FROM capability_policies WHERE store_id=? AND operation=?",
            (store["id"], operation),
        ).fetchone()
    if not row:
        return {**view, "reason": "not_installed"}
    view.update(
        detected=True,
        installed=True,
        tested=_tests_passed(row["test_evidence"]),
        deployed=bool(row["deployment_evidence"]),
        contract_digest=row["contract_digest"],
        binding_digest=row["binding_digest"],
    )
    if not view["deployed"]:
        return {**view, "reason": "not_deployed"}
    if not view["tested"]:
        return {**view, "reason": "tests_not_passed"}
    if row["contract_digest"] != contracts().REGISTRY_DIGEST:
        return {**view, "reason": "contract_digest_mismatch"}
    merchant_enabled = bool(policy and policy["enabled"])
    view["revision"] = policy["revision"] if policy else 0
    if not merchant_enabled:
        return {**view, "reason": "disabled_by_merchant"}
    return {**view, "enabled": True}


def capability_enabled(dbs, store, operation: str) -> bool:
    """Server-side effective flag; consulted on EVERY dispatch."""
    return operation_states(dbs, store, operation)["enabled"]


def effective_listing(dbs, store) -> list[dict]:
    return [operation_states(dbs, store, operation) for operation in sorted(BY_OPERATION)]


def attach_capability_controls(app, dbs):
    state = app.state
    prefix = "/api/commerce/stores/{store_id}/capabilities"

    @app.get(prefix + "/effective")
    def effective(store_id: str, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        return {"capabilities": effective_listing(dbs, store)}

    @app.post(prefix + "/{operation}/enable")
    def enable(store_id: str, operation: str, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        states = operation_states(dbs, store, operation)
        reason = states["reason"]
        if reason == "unsupported_operation":
            raise HTTPException(422, "Unknown canonical capability")
        if reason and reason != "disabled_by_merchant":
            raise HTTPException(
                409,
                {
                    "message": "Capability prerequisites are not met; it cannot be enabled",
                    "reason": reason,
                    "states": states,
                },
            )
        revision = set_capability_policy(dbs, store_id, operation, True, actor["id"])
        dbs.event(actor["org"], store_id, actor["id"], "capability.enabled",
                  {"operation": operation, "revision": revision})
        return operation_states(dbs, store, operation)

    @app.post(prefix + "/{operation}/disable")
    def disable(store_id: str, operation: str, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        if operation not in BY_OPERATION:
            raise HTTPException(422, "Unknown canonical capability")
        # Commits immediately: issuance of new executions stops at the next
        # dispatch check; already-dispatched calls may complete (§14.5).
        revision = set_capability_policy(dbs, store_id, operation, False, actor["id"])
        dbs.event(actor["org"], store_id, actor["id"], "capability.disabled",
                  {"operation": operation, "revision": revision})
        states = operation_states(dbs, store, operation)
        return {**states, "in_flight": "already-dispatched calls may complete"}
