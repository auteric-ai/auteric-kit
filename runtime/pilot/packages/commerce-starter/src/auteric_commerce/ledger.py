import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .domain import DomainError, enforce_policy


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Ledger:
    def __init__(self, settings, connector):
        self.settings, self.connector = settings, connector
        Path(settings.database).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS changes (
                id TEXT PRIMARY KEY, merchant TEXT NOT NULL, request_key TEXT NOT NULL,
                request TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
                state TEXT NOT NULL, creator TEXT NOT NULL, approver TEXT,
                expires REAL NOT NULL, UNIQUE(merchant, request_key));
              CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL,
                change_id TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.settings.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, change_id, actor, event):
        db.execute("INSERT INTO events(merchant,change_id,actor,event,at) VALUES(?,?,?,?,?)",
                   (self.settings.merchant_id, change_id, actor, event, time.time()))

    def row(self, db, change_id):
        row = db.execute("SELECT * FROM changes WHERE id=? AND merchant=?",
                         (change_id, self.settings.merchant_id)).fetchone()
        if row is None:
            raise DomainError("Change not found", 404)
        return row

    def present(self, row):
        return {"id": row["id"], "state": row["state"], "digest": row["digest"],
                "proposal": json.loads(row["body"]), "created_by": row["creator"],
                "approved_by": row["approver"], "expires_at": row["expires"]}

    def get(self, change_id):
        with self.connection() as db:
            return self.present(self.row(db, change_id))

    def list(self):
        with self.connection() as db:
            return [self.present(row) for row in db.execute(
                "SELECT * FROM changes WHERE merchant=? ORDER BY rowid DESC LIMIT 100", (self.settings.merchant_id,))]

    def stage(self, request, actor):
        payload = request.model_dump(mode="json")
        # Decimal text is normalized so retries with 10 and 10.00 are equivalent.
        payload["new_price"] = format(request.new_price, ".2f")
        request_json = canonical(payload)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM changes WHERE merchant=? AND request_key=?",
                                  (self.settings.merchant_id, request.idempotency_key)).fetchone()
            if existing:
                if existing["request"] != request_json or existing["creator"] != actor:
                    raise DomainError("Idempotency key was already used for another request")
                return self.present(existing)
            for pending in db.execute("SELECT body FROM changes WHERE merchant=? AND state IN ('applying','uncertain')", (self.settings.merchant_id,)):
                if json.loads(pending['body'])['variant']['id'] == request.variant_id:
                    raise DomainError("Variant has an unresolved write; operator reconciliation required")
            variant = self.connector.get(request.variant_id)
            enforce_policy(self.settings, variant, request.new_price)
            body = {"merchant_id": self.settings.merchant_id, "variant": variant.model_dump(mode="json"),
                    "new_price": payload["new_price"], "reason": request.reason,
                    "created_at": time.time(), "mode": self.settings.mode, "shop_domain": self.settings.shop_domain}
            body_json = canonical(body)
            digest = hashlib.sha256(body_json.encode()).hexdigest()
            change_id = uuid.uuid4().hex
            db.execute("INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (change_id, self.settings.merchant_id, request.idempotency_key, request_json,
                        body_json, digest, "staged", actor, None, time.time()+self.settings.proposal_ttl_seconds))
            self.event(db, change_id, actor, "staged")
            return self.present(self.row(db, change_id))

    def approve(self, change_id, digest, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, change_id)
            if row["state"] != "staged" or row["expires"] < time.time():
                raise DomainError("Change is not staged or has expired")
            if row["digest"] != digest or row["creator"] == actor:
                raise DomainError("Approval must match the exact proposal and a separate principal", 403)
            db.execute("UPDATE changes SET state='approved', approver=? WHERE id=?", (actor, change_id))
            self.event(db, change_id, actor, "approved")
        return self.get(change_id)

    def discard(self, change_id, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, change_id)
            if row["state"] not in {"staged", "approved"}:
                raise DomainError("Only staged/approved changes can be discarded")
            db.execute("UPDATE changes SET state='discarded' WHERE id=?", (change_id,))
            self.event(db, change_id, actor, "discarded")
        return self.get(change_id)

    def execute(self, change_id, actor):
        if self.settings.mode == "shopify" and not self.settings.live_writes:
            raise DomainError("Live Shopify writes are disabled", 403)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, change_id)
            if row["state"] == "applied":
                return self.present(row)  # Never repeat the platform mutation.
            if row["state"] != "approved" or row["expires"] < time.time():
                raise DomainError("Fresh separate approval required; uncertain operations cannot be retried")
            body = json.loads(row["body"])
            for pending in db.execute("SELECT body FROM changes WHERE merchant=? AND state IN ('applying','uncertain') AND id<>?", (self.settings.merchant_id, change_id)):
                if json.loads(pending['body'])['variant']['id'] == body['variant']['id']:
                    raise DomainError("Variant has an unresolved write; operator reconciliation required")
            if body["mode"] != self.settings.mode or body.get("shop_domain") != self.settings.shop_domain:
                raise DomainError("Connector binding changed; create a new proposal")
            db.execute("UPDATE changes SET state='applying' WHERE id=?", (change_id,))
            self.event(db, change_id, actor, "applying")
        try:
            current = self.connector.get(body["variant"]["id"])
            expected = body["variant"]
            if any(str(getattr(current, k)) != str(expected[k]) for k in ("id", "product_id", "sku", "currency")) or current.price != Decimal(expected["price"]):
                raise DomainError("Catalog changed since approval; create a new proposal")
            enforce_policy(self.settings, current, Decimal(body["new_price"]))
        except Exception:
            self.finish(change_id, actor, "stale")
            raise DomainError("Pre-execution validation failed; create a fresh proposal") from None
        try:
            self.connector.set_price(current, Decimal(body["new_price"]))
            if self.connector.get(current.id).price != Decimal(body["new_price"]):
                raise DomainError("Post-write verification failed")
        except Exception:
            self.finish(change_id, actor, "uncertain")
            raise DomainError("Write outcome uncertain; inspect Shopify and audit log. No automatic retry.", 502) from None
        self.finish(change_id, actor, "applied")
        return self.get(change_id)

    def finish(self, change_id, actor, state):
        with self.connection() as db:
            db.execute("UPDATE changes SET state=? WHERE id=? AND merchant=?", (state, change_id, self.settings.merchant_id))
            self.event(db, change_id, actor, state)

    def audit(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events WHERE merchant=? ORDER BY sequence DESC LIMIT 200", (self.settings.merchant_id,))]
