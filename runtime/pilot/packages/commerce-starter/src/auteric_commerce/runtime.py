"""Durable commerce authorization. SQLite is a local single-service store, not distributed consensus."""
import asyncio
import hashlib
import inspect
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .actions import CommerceAction
from .domain import DomainError, Variant
from .policy import PolicyConfig, evaluate

logger = logging.getLogger("auteric.audit")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


async def invoke(fn, *args):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    result = await asyncio.to_thread(fn, *args)
    return await result if inspect.isawaitable(result) else result


class Runtime:
    """Trusted host API: authentication/approver-role checks belong to the calling host.

    Never give this object, backend credentials, or the SQLite file to an agent.
    An uncertain external write cannot safely be retried and requires reconciliation.
    """

    def __init__(self, database, merchant_id, backend, policy=None, ttl_seconds=900,
                 binding=None, live_writes=True, context_timeout_seconds=30):
        self.database, self.merchant_id, self.backend = str(database), merchant_id, backend
        self.policy = policy or PolicyConfig()
        self.ttl_seconds, self.binding, self.live_writes = ttl_seconds, binding or {}, live_writes
        self.context_timeout_seconds = context_timeout_seconds
        if context_timeout_seconds <= 0:
            raise ValueError("Context timeout must be positive")
        if not merchant_id or ttl_seconds <= 0 or self.database == ":memory:":
            raise ValueError("A merchant, positive TTL and persistent SQLite path are required")
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS runtime_actions (
                merchant TEXT NOT NULL, id TEXT NOT NULL, request_key TEXT NOT NULL,
                request TEXT NOT NULL, action TEXT NOT NULL, decision TEXT NOT NULL,
                binding TEXT NOT NULL, policy TEXT NOT NULL, digest TEXT NOT NULL,
                state TEXT NOT NULL, creator TEXT NOT NULL, approver TEXT,
                expires REAL NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(merchant,id), UNIQUE(merchant,request_key));
              CREATE TABLE IF NOT EXISTS runtime_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL,
                action_id TEXT NOT NULL, trace_id TEXT NOT NULL, actor TEXT NOT NULL,
                event TEXT NOT NULL, state TEXT NOT NULL, at REAL NOT NULL);
              CREATE TRIGGER IF NOT EXISTS runtime_events_no_update
                BEFORE UPDATE ON runtime_events BEGIN SELECT RAISE(ABORT,'append-only audit'); END;
              CREATE TRIGGER IF NOT EXISTS runtime_events_no_delete
                BEFORE DELETE ON runtime_events BEGIN SELECT RAISE(ABORT,'append-only audit'); END;
            ''')
            if "payload" not in {column[1] for column in db.execute("PRAGMA table_info(runtime_events)")}:
                db.execute("ALTER TABLE runtime_events ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")

    def with_backend(self, backend, binding):
        return Runtime(self.database, self.merchant_id, backend, self.policy,
                       self.ttl_seconds, binding, self.live_writes, self.context_timeout_seconds)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _row(self, db, action_id):
        row = db.execute("SELECT * FROM runtime_actions WHERE merchant=? AND id=?",
                         (self.merchant_id, action_id)).fetchone()
        if row is None:
            raise DomainError("Action not found", 404)
        return row

    @staticmethod
    def _present(row):
        return {"id": row["id"], "state": row["state"], "digest": row["digest"],
                "action": json.loads(row["action"]), "decision": json.loads(row["decision"]),
                "created_by": row["creator"], "approved_by": row["approver"],
                "expires_at": row["expires"], "created_at": row["created"]}

    def _event(self, db, row, actor, event):
        action = json.loads(row["action"])
        entry = {"merchant": self.merchant_id, "action_id": row["id"],
                 "trace_id": action["trace_id"], "actor": actor, "event": event,
                 "state": row["state"], "at": time.time()}
        decision = json.loads(row["decision"])
        # Explicit allow-list: free text, metadata, descriptions and backend exceptions
        # never enter event logs. IDs/roles must be non-secret host identifiers.
        context = decision.get("context", {})
        payload = {"principal": action["principal"], "agent": action["agent"],
                   "app_id": action.get("app_id"), "delegation": action.get("delegation"),
                   "type": action["type"], "items": [{k: v for k, v in item.items() if k != "title"} for item in action["items"]],
                   "context": {k: v for k, v in context.items() if k != "items"},
                   "item_context": [{k: v for k, v in item.items() if k != "title"} for item in context.get("items", [])],
                   "decision": decision["outcome"], "matched_rules": decision["matched_rules"],
                   "reasons": decision["reasons"], "approved_by": row["approver"],
                   "expires_at": row["expires"], "digest": row["digest"]}
        entry["payload"] = canonical(payload)
        db.execute("INSERT INTO runtime_events(merchant,action_id,trace_id,actor,event,state,at,payload) "
                   "VALUES(:merchant,:action_id,:trace_id,:actor,:event,:state,:at,:payload)", entry)
        # Do not emit untrusted intent, descriptions, metadata, credentials or backend errors.
        logger.info(canonical({**entry, "payload": payload}))

    def get_action(self, action_id):
        with self.connection() as db:
            return self._present(self._row(db, action_id))

    def list_actions(self, state=None, limit=100):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM runtime_actions WHERE merchant=? "
                              "AND (? IS NULL OR state=?) ORDER BY created DESC LIMIT ?",
                              (self.merchant_id, state, state, max(1, min(limit, 500))))
            return [self._present(row) for row in rows]

    def audit(self, action_id=None, limit=200):
        with self.connection() as db:
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM runtime_events WHERE merchant=? AND (? IS NULL OR action_id=?) "
                "ORDER BY sequence DESC LIMIT ?", (self.merchant_id, action_id, action_id, max(1, min(limit, 1000))))]

    def _fingerprint(self, action, decision, binding, policy):
        return hashlib.sha256(canonical({"action": action, "decision": decision,
                                          "binding": binding, "policy": policy}).encode()).hexdigest()

    def _existing(self, db, action, request):
        rows = db.execute("SELECT * FROM runtime_actions WHERE merchant=? AND (id=? OR request_key=?)",
                          (self.merchant_id, action.action_id, action.idempotency_key)).fetchall()
        for row in rows:
            if row["request"] != request:
                raise DomainError("Action ID/idempotency key belongs to a different exact request")
        return rows[0] if rows else None

    @staticmethod
    def _targets(action):
        return {item["resource_id"] for item in action["items"]}

    def _unresolved(self, db, action, exclude=None):
        for row in db.execute("SELECT id,action FROM runtime_actions WHERE merchant=? "
                              "AND state IN ('executing','uncertain')", (self.merchant_id,)):
            if row["id"] != exclude and self._targets(json.loads(row["action"])) & self._targets(action):
                raise DomainError("Resource has an unresolved write; reconcile before creating or executing another action")

    async def _enrich(self, action):
        enriched = action.model_copy(deep=True)
        if str(action.type.value) in {"price.update", "price.read"}:
            for item in enriched.items:
                variant = await invoke(self.backend.get, item.resource_id)
                variant = Variant.model_validate(variant)
                if variant.id != item.resource_id:
                    raise DomainError("Backend returned a different resource", 422)
                item.before, item.sku, item.currency = variant.price, variant.sku, variant.currency
                item.inventory_quantity, item.product_id, item.title = variant.stock, variant.product_id, variant.title
        return CommerceAction.model_validate(enriched.model_dump(mode="python"))

    async def evaluate(self, action):
        action = CommerceAction.model_validate(action)
        if action.merchant_id != self.merchant_id:
            raise DomainError("Merchant binding mismatch", 403)
        request = canonical(action.model_dump(mode="json"))
        with self.connection() as db:
            existing = self._existing(db, action, request)
            if existing:
                return self._present(existing)
            self._unresolved(db, action.model_dump(mode="json"))
        try:
            enriched = await asyncio.wait_for(self._enrich(action), self.context_timeout_seconds)
            decision = evaluate(enriched, self.policy).model_dump(mode="json")
        except Exception:
            # Keep the attempted target/proposal, but remove all claimed business state.
            # A failed read must not manufacture a zero-risk or trusted context.
            enriched = action.model_copy(deep=True)
            for item in enriched.items:
                item.before = item.sku = item.currency = item.inventory_quantity = item.product_id = item.title = None
            decision = {"outcome": "BLOCK", "reasons": ["Authoritative context/policy evaluation failed; no action executed. Retry requires a new action."],
                        "matched_rules": ["context-unavailable"], "context": {"verified": False, "items": [], "affected_resource_count": len(action.items)}}
        body = enriched.model_dump(mode="json")
        policy = self.policy.model_dump(mode="json")
        digest = self._fingerprint(body, decision, self.binding, policy)
        state = {"ALLOW": "allowed", "BLOCK": "blocked", "REQUIRE_APPROVAL": "waiting_for_approval"}[decision["outcome"]]
        creator = enriched.principal.subject or "unknown"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._existing(db, action, request)
            if existing:
                return self._present(existing)
            self._unresolved(db, body)
            db.execute("INSERT INTO runtime_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (self.merchant_id, action.action_id, action.idempotency_key, request, canonical(body),
                        canonical(decision), canonical(self.binding), canonical(policy), digest, state, creator,
                        None, time.time() + min(self.ttl_seconds, self.policy.approval_ttl_seconds), time.time()))
            row = self._row(db, action.action_id)
            for event in ("action.received", "action.normalized", "policy.evaluated",
                          {"allowed": "action.allowed", "blocked": "action.blocked", "waiting_for_approval": "approval.requested"}[state]):
                self._event(db, row, creator, event)
            return self._present(row)

    def approve(self, action_id, digest, actor):
        expired = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, action_id)
            if row["state"] != "waiting_for_approval":
                raise DomainError("Action is not waiting for approval")
            if not actor or not actor.strip() or actor.strip().lower() in {"unknown", "anonymous"} or actor == row["creator"] or digest != row["digest"]:
                raise DomainError("Approval requires a separate identified principal and exact action digest", 403)
            expired = row["expires"] <= time.time()
            db.execute("UPDATE runtime_actions SET state=?,approver=? WHERE merchant=? AND id=?",
                       ("expired" if expired else "approved", None if expired else actor, self.merchant_id, action_id))
            self._event(db, self._row(db, action_id), actor, "action.expired" if expired else "approval.approved")
        if expired:
            raise DomainError("Action expired; evaluate a new action")
        return self.get_action(action_id)

    def reject(self, action_id, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, action_id)
            if not actor or not actor.strip() or actor.strip().lower() in {"unknown", "anonymous"}:
                raise DomainError("Identified rejecting principal required", 403)
            if row["state"] not in {"allowed", "waiting_for_approval", "approved"}:
                raise DomainError("Action cannot be rejected in this state")
            db.execute("UPDATE runtime_actions SET state='rejected' WHERE merchant=? AND id=?", (self.merchant_id, action_id))
            self._event(db, self._row(db, action_id), actor, "approval.rejected")
        return self.get_action(action_id)

    def _finish(self, action_id, actor, state):
        with self.connection() as db:
            db.execute("UPDATE runtime_actions SET state=? WHERE merchant=? AND id=?", (state, self.merchant_id, action_id))
            self._event(db, self._row(db, action_id), actor, "action." + state)

    async def execute(self, action_id, actor):
        if not actor or not actor.strip() or actor.strip().lower() in {"unknown", "anonymous"}:
            raise DomainError("Identified executor required", 403)
        if not self.live_writes:
            raise DomainError("Merchant writes are disabled", 403)
        expired = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, action_id)
            if row["state"] == "executed":
                return self._present(row)
            if row["state"] not in {"allowed", "approved"}:
                raise DomainError("Action is not authorized or has unresolved execution; no automatic retry")
            body, decision = json.loads(row["action"]), json.loads(row["decision"])
            if body["type"] != "price.update" or len(body["items"]) != 1:
                raise DomainError("Only single-resource price.update execution is supported; bulk is evaluation-only", 422)
            if row["binding"] != canonical(self.binding) or row["policy"] != canonical(self.policy.model_dump(mode="json")):
                raise DomainError("Backend/policy binding changed; evaluate a new action")
            if row["digest"] != self._fingerprint(body, decision, self.binding, self.policy.model_dump(mode="json")):
                raise DomainError("Stored action integrity validation failed", 403)
            self._unresolved(db, body, action_id)
            expired = row["expires"] <= time.time()
            db.execute("UPDATE runtime_actions SET state=? WHERE merchant=? AND id=?",
                       ("expired" if expired else "executing", self.merchant_id, action_id))
            self._event(db, self._row(db, action_id), actor, "action.expired" if expired else "action.executing")
        if expired:
            raise DomainError("Action expired; evaluate a new action")
        try:
            action = CommerceAction.model_validate(body)
            current = await asyncio.wait_for(self._enrich(action), self.context_timeout_seconds)
            if current.model_dump(mode="json") != body:
                raise DomainError("Authoritative merchant state changed")
            if row["binding"] != canonical(self.binding) or row["policy"] != canonical(self.policy.model_dump(mode="json")):
                raise DomainError("Backend or policy changed during validation")
            latest = evaluate(current, self.policy).model_dump(mode="json")
            if latest != decision:
                raise DomainError("Authority or policy decision changed")
            item = current.items[0]
            variant = Variant(id=item.resource_id, product_id=item.product_id, title=item.title,
                              sku=item.sku, price=item.before, currency=item.currency, stock=item.inventory_quantity)
        except BaseException as error:
            self._finish(action_id, actor, "stale")
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise DomainError("Pre-execution state or authority changed/unavailable; evaluate a new action") from None
        try:
            await invoke(self.backend.set_price, variant, item.proposed_after)
            after = Variant.model_validate(await invoke(self.backend.get, item.resource_id))
            if after.price != item.proposed_after or after.id != variant.id or after.product_id != variant.product_id:
                raise DomainError("Post-write verification failed")
        except BaseException as error:
            self._finish(action_id, actor, "uncertain")
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise DomainError("Write outcome uncertain; reconcile merchant state. Automatic retries are blocked.", 502) from None
        self._finish(action_id, actor, "executed")
        return self.get_action(action_id)
