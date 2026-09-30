"""Adapter from the existing durable-job polling worker dispatch to MerchantTransport.

Thin wrapper only: execution still flows through the unchanged durable jobs
queue consumed by `/api/commerce/edge/poll`; no behavior change.
"""

from __future__ import annotations

import time

from .base import MerchantTransport, TransportAction, TransportResult


class OutboundWorkerTransport(MerchantTransport):
    def __init__(self, dispatch, dbs=None):
        # dispatch is the existing app.state.dispatch job-queue path.
        self._dispatch = dispatch
        self._dbs = dbs

    async def execute(self, installation, action: TransportAction, *, mapping=None) -> TransportResult:
        result = await self._dispatch(
            installation.store_id, action.operation, action.input, mapping, key=action.action_id
        )
        return TransportResult(outcome="completed", action_id=action.action_id, result=result)

    async def inspect_health(self, installation) -> dict:
        observed_at = time.time()
        if installation.revoked_at:
            return {"reachable": False, "reason": "revoked", "observed_at": observed_at}
        heartbeat = None
        release_id = installation.release_id
        if self._dbs is not None:
            with self._dbs.db() as db:
                row = db.execute(
                    "SELECT heartbeat,connector_release FROM stores WHERE id=?",
                    (installation.store_id,),
                ).fetchone()
            if row:
                heartbeat, release_id = row["heartbeat"], row["connector_release"] or release_id
        return {
            "reachable": bool(heartbeat and heartbeat > observed_at - 40),
            "last_heartbeat": heartbeat,
            "contract_digest": None,
            "release_id": release_id,
            "observed_at": observed_at,
        }

    async def reconcile(self, installation, action_id: str) -> dict | None:
        if self._dbs is None:
            return None
        with self._dbs.db() as db:
            row = db.execute(
                "SELECT id,operation,state,created,claimed FROM jobs WHERE id=? AND store=?",
                (action_id, installation.store_id),
            ).fetchone()
        if not row:
            return None
        return {
            "action_id": row["id"],
            "operation": row["operation"],
            "outcome": {"completed": "completed", "uncertain": "uncertain"}.get(row["state"], "failed")
            if row["state"] in {"completed", "failed", "uncertain"}
            else "dispatched",
            "job_state": row["state"],
            "created_at": row["created"],
            "completed_at": None,
        }
