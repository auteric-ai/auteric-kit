"""Publish narrow, authenticated runtime evidence to Scanner.

This bridge is intentionally one-way: the Control Plane posts a short-lived
receipt after an operator's successful Connection Test.  Scanner never obtains
merchant credentials, does not execute shopping actions, and never treats a
UCP declaration as runtime proof.
"""
from __future__ import annotations

import os
import time

import httpx


SCANNER_ACTIONS = {
    "search_products": "Search products",
    "get_product": "Read product details",
    "create_cart": "Create cart",
    "get_cart": "Read availability",
    "add_to_cart": "Add item",
    "update_cart_item": "Change quantity",
    "remove_from_cart": "Remove item",
}


def install_scanner_evidence(app, dbs):
    url = os.getenv("AUTERIC_SCANNER_READINESS_URL", "").rstrip("/")
    key = os.getenv("SCANNER_SERVICE_KEY", "")

    async def publish(store, report):
        """Post only facts established by the current controlled journey.

        Controls that require their own adversarial proof remain ``unknown``;
        that prevents this bridge from inflating the public protection score.
        """
        if not url or not key:
            return {"published": False, "reason": "Scanner evidence delivery is not configured"}
        if report.get("state") != "passed" or report.get('activation', {}).get('state') != 'active':
            return {"published": False, "reason": "Protected Agent Access is not active for this evidence"}
        passed = {stage["operation"] for stage in report.get("stages", []) if stage.get("state") == "passed"}
        actions = [label for operation, label in SCANNER_ACTIONS.items() if operation in passed]
        probes = report.get('phase1_probes') or {}
        controls = {
            "agent_authorization": "pass",
            "merchant_policy": "pass",
            "user_intent": "pass",
            "action_validation": "pass",
            "cart_integrity": "pass" if probes.get('passed') and {"create_cart", "get_cart"} <= passed else "unknown",
            "audit_trail": "pass",
            # These require dedicated negative/replay probes, not a happy-path
            # cart run.  Keep them unknown until those probes are implemented.
            "approval_workflow": "unknown",
            "replay_protection": "pass" if probes.get('passed') else "unknown",
            "idempotency": "pass" if probes.get('passed') else "unknown",
            "session_ownership": "pass" if probes.get('passed') else "unknown",
            "protected_checkout": "unknown",
        }
        if report.get('mode')=='direct':
            controls['merchant_policy']='pass' if probes.get('policy_block') else 'unknown'
            controls['agent_authorization']='pass' if probes.get('agent_authorization') else 'unknown'
            controls['replay_protection']='pass' if probes.get('replay_protection') else 'unknown'
        observed_at = report.get("evidence_observed_at", report["updated_at"])
        receipt = {
            "receipt_id": "connection_" + report["run_id"],
            "connection_id": store["id"],
            "observed_at": observed_at,
            "expires_at": min(observed_at + 24 * 3600,report.get('verification_expires_at',float('inf'))),
            "actions": actions,
            "controls": controls,
            "source": "auteric_control",
            "scope": "native_phase1",
            **({'installation_id':report['installation_id'],'configuration_digest':report['binding'],
                'run_id':report['run_id'],'operations':sorted(passed),'enforcement_path':report['path']}
                if report.get('mode')=='direct' else {}),
        }
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
                response = await client.post(
                    url + "/" + store["domain"], json=receipt,
                    headers={"X-Scanner-Service-Key": key},
                )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            dbs.event(store["org"], store["id"], "system", "scanner.runtime_evidence_failed", {"reason": str(exc)[:300]})
            return {"published": False, "reason": "Scanner did not accept current runtime evidence"}
        dbs.event(store["org"], store["id"], "system", "scanner.runtime_evidence_published", {"run_id": report["run_id"]})
        return {"published": True, "status": response.json().get("status")}

    async def invalidate(store):
        """Supersede a verified snapshot after explicit disable/revocation."""
        if not url or not key:
            return {"published": False, "reason": "Scanner evidence delivery is not configured"}
        from .storage import uid
        now = time.time()
        receipt = {"receipt_id": "disabled_" + uid(), "connection_id": store["id"],
                   "observed_at": now, "expires_at": now + 3600,
                   "actions": [], "controls": {"agent_authorization": "fail"},
                   "source": "auteric_control", "scope": "native_phase1"}
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
                response = await client.post(url + "/" + store["domain"], json=receipt,
                                             headers={"X-Scanner-Service-Key": key})
            response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            dbs.event(store["org"], store["id"], "system", "scanner.invalidation_failed")
            return {"published": False, "reason": "Access is disabled; Scanner invalidation failed"}
        return {"published": True, "status": response.json().get("status")}

    app.state.invalidate_scanner_evidence = invalidate
    app.state.publish_scanner_evidence = publish
