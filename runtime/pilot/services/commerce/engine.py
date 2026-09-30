"""Storefront lifecycle built on the existing private Runtime ledger and approvals.

Host-only API. Canonical policy decisions never come from remote clients.
"""

import json
import time

from auteric_commerce.actions import CommerceAction
from auteric_commerce.domain import DomainError
from auteric_commerce.runtime import Runtime, canonical


class StorefrontRuntime(Runtime):
    def __init__(self, database, merchant_id, backend, policy=None, ttl_seconds=900,
                 binding=None, live_writes=True, context_timeout_seconds=30):
        from .storage import Store
        if not isinstance(database, Store):
            self.shared_store = None
            super().__init__(database, merchant_id, backend, policy, ttl_seconds,
                             binding, live_writes, context_timeout_seconds)
            return
        from auteric_commerce.policy import PolicyConfig
        self.shared_store = database
        self.database = database.database_url
        self.merchant_id, self.backend = merchant_id, backend
        self.policy = policy or PolicyConfig()
        self.ttl_seconds, self.binding, self.live_writes = ttl_seconds, binding or {}, live_writes
        self.context_timeout_seconds = context_timeout_seconds

    def connection(self):
        return self.shared_store.db() if self.shared_store else super().connection()

    def _row(self, db, action_id):
        if not self.shared_store:
            return super()._row(db, action_id)
        row = db.execute('SELECT * FROM runtime_actions WHERE merchant=? AND id=? FOR UPDATE',
                         (self.merchant_id, action_id)).fetchone()
        if row is None:
            raise DomainError('Action not found', 404)
        return row

    def record(self, action: CommerceAction, decision: dict):
        body = action.model_dump(mode="json")
        request = canonical(body)
        policy = self.policy.model_dump(mode="json")
        outcome = decision["outcome"]
        state = {"ALLOW": "allowed", "BLOCK": "blocked", "REQUIRE_APPROVAL": "waiting_for_approval"}[outcome]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._existing(db, action, request)
            if existing:
                return self._present(existing)
            now = time.time()
            db.execute(
                "INSERT INTO runtime_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.merchant_id,
                    action.action_id,
                    action.idempotency_key,
                    request,
                    request,
                    canonical(decision),
                    canonical(self.binding),
                    canonical(policy),
                    self._fingerprint(body, decision, self.binding, policy),
                    state,
                    action.principal.subject or "unknown",
                    None,
                    now + self.ttl_seconds,
                    now,
                ),
            )
            row = self._row(db, action.action_id)
            for event in ("commerce.action.created", "policy.evaluated", "action." + state):
                self._event(db, row, row["creator"], event)
            return self._present(row)

    async def run(self, action_id, actor, execute, revalidate):
        """Reuse approval/fingerprint/audit semantics, execute only a trusted host callback."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, action_id)
            if row["state"] == "executed":
                return None
            if row["state"] not in {"allowed", "approved"} or row["expires"] <= time.time():
                raise DomainError("Action is blocked, expired, unapproved or has unresolved execution")
            if row["binding"] != canonical(self.binding):
                raise DomainError("Mapping/policy binding changed; evaluate a new action")
            if row["digest"] != self._fingerprint(
                json.loads(row["action"]), json.loads(row["decision"]), self.binding, json.loads(row["policy"])
            ):
                raise DomainError("Action fingerprint mismatch", 403)
            db.execute(
                "UPDATE runtime_actions SET state='executing' WHERE merchant=? AND id=?", (self.merchant_id, action_id)
            )
            self._event(db, self._row(db, action_id), actor, "action.executing")
        try:
            await revalidate()
        except BaseException:
            self._finish(action_id, actor, "stale")
            raise
        try:
            result = await execute()
        except BaseException:
            self._finish(action_id, actor, "uncertain")
            raise
        self._finish(action_id, actor, "executed")
        return result
