"""Durable, reviewable merchant connection runs.

This is deliberately separate from ``connection_runs``.  That table records an
operator's controlled cart/checkout test journey; these records describe the
long-lived merchant onboarding journey and must never be treated as proof that
a shopper transaction ran.
"""
from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .storage import digest, encode, uid


RUN_STATES = (
    "created", "repository_connected", "authority_selected", "inventory_complete",
    "gap_analysis_complete", "changes_prepared", "awaiting_review", "changes_approved",
    "local_changes_applied", "pull_request_open", "awaiting_merge", "verified_local", "deployment_pending", "verified_public",
    "claimed", "verified_runtime", "activated",
)
EXCEPTION_STATES = frozenset({"needs_review", "failed", "blocked", "interrupted", "superseded"})
TERMINAL_STATES = frozenset({"activated", "failed", "blocked", "superseded"})
ALLOWED_NEXT = {
    "created": {"repository_connected"}, "repository_connected": {"authority_selected"},
    "authority_selected": {"inventory_complete"}, "inventory_complete": {"gap_analysis_complete"},
    "gap_analysis_complete": {"changes_prepared"}, "changes_prepared": {"awaiting_review"},
    "awaiting_review": {"changes_approved"},
    # A reviewed local-agent change and a reviewed GitHub pull request take
    # different paths, but converge before public verification.
    "changes_approved": {"local_changes_applied", "pull_request_open"},
    "local_changes_applied": {"verified_local"}, "pull_request_open": {"awaiting_merge"},
    "awaiting_merge": {"verified_local"}, "verified_local": {"deployment_pending"},
    "deployment_pending": {"verified_public"}, "verified_public": {"claimed"},
    "claimed": {"verified_runtime"}, "verified_runtime": {"activated"},
}

CAPABILITY_STATES = (
    "candidate", "implemented", "verified_local", "verified_public",
    "verified_runtime", "activated", "blocked", "unsupported",
)
CAPABILITY_NEXT = dict(zip(CAPABILITY_STATES[:6], CAPABILITY_STATES[1:6]))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityCandidate(StrictModel):
    """An arbitrary merchant API candidate, never an immediately exposed tool."""
    id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    display_name: str = Field(min_length=1, max_length=200)
    source: dict = Field(default_factory=dict)
    operation_hint: str | None = Field(default=None, max_length=160)
    side_effect: Literal["read", "write", "unknown"] = "unknown"
    adapter_id: str | None = Field(default=None, max_length=160)


class CreateOnboardingRun(StrictModel):
    repository: str = Field(min_length=1, max_length=500)
    commit_sha: str | None = Field(default=None, min_length=7, max_length=128)
    domain: str | None = Field(default=None, min_length=3, max_length=253)
    entry_point: Literal["coding_agent", "github", "builder_remote_mcp"]
    capabilities: list[CapabilityCandidate] = Field(default_factory=list, max_length=500)


class RunTransition(StrictModel):
    state: str = Field(min_length=1, max_length=64)
    evidence: dict = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=1000)


class CapabilityTransition(StrictModel):
    state: str = Field(min_length=1, max_length=64)
    evidence: dict = Field(default_factory=dict)


def github_contract() -> dict:
    """Describe the unimplemented external boundary without pretending OAuth works."""
    return {
        "state": "pending_integration",
        "available": False,
        "reason": "GitHub App/OAuth, webhook verification, and repository write credentials are not configured by this service.",
        "required_before_enablement": [
            "GitHub App installation and OAuth callback", "verified webhook delivery",
            "least-privilege repository access", "merchant-reviewed branch and pull-request creation",
        ],
    }


def _request_key(body: CreateOnboardingRun) -> str:
    # Same merchant/repository/commit/domain/entry point is exactly one run.
    return digest(encode({
        "repository": body.repository, "commit_sha": body.commit_sha,
        "domain": body.domain, "entry_point": body.entry_point,
    }))


def _read(db, store_id: str, run_id: str) -> dict:
    row = db.execute("SELECT body FROM onboarding_runs WHERE id=? AND store=?", (run_id, store_id)).fetchone()
    if not row:
        raise HTTPException(404, "Onboarding run not found")
    return json.loads(row["body"])


def _save(db, report: dict) -> None:
    report["updated_at"] = time.time()
    db.execute("UPDATE onboarding_runs SET state=?,body=?,updated=? WHERE id=? AND store=?",
               (report["state"], encode(report), report["updated_at"], report["run_id"], report["store_id"]))


def attach_onboarding_runs(app, dbs):
    state = app.state
    root = "/api/commerce/stores/{store_id}/onboarding-runs"

    @app.get(root + "/github-contract")
    def github_status(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        return {"github": github_contract()}

    @app.post(root)
    def create(store_id: str, body: CreateOnboardingRun, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        request_key = _request_key(body)
        now = time.time()
        run_id = uid()
        with dbs.db() as db:
            inserted = db.execute(
                "INSERT INTO onboarding_run_requests(store,request_key,run_id,created) VALUES(?,?,?,?) "
                "ON CONFLICT(store,request_key) DO NOTHING", (store_id, request_key, run_id, now)
            ).rowcount
            if not inserted:
                existing = db.execute("SELECT run_id FROM onboarding_run_requests WHERE store=? AND request_key=?",
                                      (store_id, request_key)).fetchone()
                if not existing:
                    raise HTTPException(409, "Onboarding request is being created; retry safely")
                report = _read(db, store_id, existing["run_id"])
                return {**report, "idempotent": True}
            report = {
                "kind": "merchant_onboarding", "run_id": run_id, "store_id": store_id,
                "actor": actor["id"], "state": "created", "entry_point": body.entry_point,
                "repository": body.repository, "commit_sha": body.commit_sha, "domain": body.domain,
                "request_key": request_key, "created_at": now, "updated_at": now,
                "history": [{"from": None, "to": "created", "at": now, "evidence": {}}],
                "github": github_contract() if body.entry_point == "github" else None,
                "capabilities": [{
                    "id": candidate.id, "display_name": candidate.display_name,
                    "source": candidate.source, "operation_hint": candidate.operation_hint,
                    "side_effect": candidate.side_effect, "adapter_id": candidate.adapter_id,
                    "adapter_status": "registered" if candidate.adapter_id else "awaiting_adapter",
                    "state": "candidate", "eligible_for_mcp": False,
                    # Actual MCP/UCP exposure remains owned by active mappings,
                    # capability controls and the runtime gateway. This durable
                    # inventory is intentionally not an exposure switch.
                    "actual_mcp_exposure": False,
                    "history": [{"to": "candidate", "at": now, "evidence": {}}],
                } for candidate in body.capabilities],
            }
            db.execute("INSERT INTO onboarding_runs(id,store,actor,request_key,state,body,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, store_id, actor["id"], request_key, "created", encode(report), now, now))
        dbs.event(actor["org"], store_id, actor["id"], "onboarding_run.created",
                  {"run_id": run_id, "entry_point": body.entry_point, "capability_count": len(body.capabilities)})
        return {**report, "idempotent": False}

    @app.get(root)
    def list_runs(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            return {"runs": [json.loads(row["body"]) for row in db.execute(
                "SELECT body FROM onboarding_runs WHERE store=? ORDER BY created DESC LIMIT 100", (store_id,))]}

    @app.get(root + "/{run_id}")
    def read(store_id: str, run_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            return _read(db, store_id, run_id)

    @app.post(root + "/{run_id}/transition")
    def transition(store_id: str, run_id: str, body: RunTransition, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            report = _read(db, store_id, run_id)
            if report["actor"] != actor["id"]:
                raise HTTPException(403, "Only the onboarding initiator can advance this run")
            current, target = report["state"], body.state
            if current == target:
                return {**report, "idempotent": True}
            if current in TERMINAL_STATES:
                raise HTTPException(409, "A terminal onboarding run cannot transition")
            expected = sorted(ALLOWED_NEXT.get(current, set()))
            allowed_exception = target in EXCEPTION_STATES and target != "interrupted"
            if target not in expected and not allowed_exception:
                raise HTTPException(409, {"message": "Illegal onboarding state transition", "from": current,
                                          "to": target, "expected": expected})
            if target == "pull_request_open" and report["entry_point"] == "github":
                # There is intentionally no simulated GitHub App/OAuth flow.
                # A controller may retain discovery state, but may not claim a PR
                # was opened until a real integration is implemented.
                raise HTTPException(409, github_contract())
            if target == "activated":
                selected = [cap for cap in report["capabilities"] if cap["adapter_status"] == "registered"]
                if not selected or any(cap["state"] != "verified_runtime" for cap in selected):
                    raise HTTPException(409, "Activation requires at least one adapter-backed capability with runtime verification")
                # Activation is atomic for the selected adapter-backed subset. Other
                # discovered APIs remain retained candidates until an adapter exists.
                for capability in selected:
                    capability["state"] = "activated"
                    capability["eligible_for_mcp"] = True
                    capability["history"].append({"from": "verified_runtime", "to": "activated",
                                                  "at": time.time(), "evidence": {"run_activation": True}})
            report["state"] = target
            report["history"].append({"from": current, "to": target, "at": time.time(),
                                      "reason": body.reason, "evidence": body.evidence})
            _save(db, report)
        dbs.event(actor["org"], store_id, actor["id"], "onboarding_run.transitioned",
                  {"run_id": run_id, "from": current, "to": target})
        return {**report, "idempotent": False}

    @app.post(root + "/{run_id}/resume")
    def resume(store_id: str, run_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            report = _read(db, store_id, run_id)
            if report["actor"] != actor["id"] or report["state"] != "interrupted":
                raise HTTPException(409, "Only an interrupted run started by this operator can resume")
            previous = next((event["from"] for event in reversed(report["history"]) if event["to"] == "interrupted"), None)
            if previous not in RUN_STATES:
                raise HTTPException(409, "Interrupted run has no safe resumable state")
            report["state"] = previous
            report["history"].append({"from": "interrupted", "to": previous, "at": time.time(), "evidence": {}})
            _save(db, report)
        return report

    @app.post(root + "/{run_id}/capabilities/{capability_id}/transition")
    def transition_capability(store_id: str, run_id: str, capability_id: str, body: CapabilityTransition,
                              actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            report = _read(db, store_id, run_id)
            if report["actor"] != actor["id"]:
                raise HTTPException(403, "Only the onboarding initiator can advance this capability")
            capability = next((item for item in report["capabilities"] if item["id"] == capability_id), None)
            if not capability:
                raise HTTPException(404, "Onboarding capability not found")
            current, target = capability["state"], body.state
            if current == target:
                return {**report, "idempotent": True}
            if target in {"blocked", "unsupported"} and current not in {"activated", "blocked", "unsupported"}:
                pass
            elif target != CAPABILITY_NEXT.get(current):
                raise HTTPException(409, {"message": "Illegal capability lifecycle transition", "from": current,
                                          "to": target, "expected": CAPABILITY_NEXT.get(current)})
            if target == "activated":
                if report["state"] != "activated" or capability["adapter_status"] != "registered":
                    raise HTTPException(409, "MCP exposure requires an active run and a registered adapter")
                capability["eligible_for_mcp"] = True
            capability["state"] = target
            capability["history"].append({"from": current, "to": target, "at": time.time(), "evidence": body.evidence})
            _save(db, report)
        dbs.event(actor["org"], store_id, actor["id"], "onboarding_capability.transitioned",
                  {"run_id": run_id, "capability_id": capability_id, "from": current, "to": target})
        return {**report, "idempotent": False}
