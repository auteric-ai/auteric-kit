"""Restricted exact Native receipts; telemetry is never an execution receipt."""
import json
import time

from .storage import encode


def save_receipt(dbs, installation, action, request_hash, result, *, outcome="completed"):
    with dbs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM execution_actions WHERE installation_id=? AND action_id=?",
                         (installation.id, action.action_id)).fetchone()
        if not row or any(row[key] != value for key, value in {
            "store_id": installation.store_id, "principal": action.principal,
            "operation": action.operation, "request_hash": request_hash,
        }.items()):
            raise ValueError("receipt authority does not match execution reservation")
        db.execute("INSERT INTO execution_receipts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                   (installation.id, action.action_id, installation.store_id, action.principal,
                    action.operation, request_hash, encode(result), time.time()))
        prior = db.execute("SELECT result FROM execution_receipts WHERE installation_id=? AND action_id=?",
                           (installation.id, action.action_id)).fetchone()
        if json.loads(prior["result"]) != result:
            raise ValueError("immutable receipt conflict")
        db.execute("UPDATE execution_actions SET outcome=?,completed_at=? WHERE installation_id=? AND action_id=?",
                   (outcome, time.time(), installation.id, action.action_id))


def get_receipt(dbs, installation_id, action_id):
    with dbs.db() as db:
        row = db.execute("SELECT * FROM execution_receipts WHERE installation_id=? AND action_id=?",
                         (installation_id, action_id)).fetchone()
    return dict(row) if row else None


def gateway_receipt(db, store_id, action_id):
    """Join to durable authority, never guess an installation by a public ID."""
    return db.execute("SELECT r.* FROM execution_receipts r JOIN execution_actions a "
                      "ON a.installation_id=r.installation_id AND a.action_id=r.action_id "
                      "WHERE r.store_id=? AND r.action_id=? AND a.store_id=r.store_id "
                      "AND a.principal=r.principal AND a.operation=r.operation "
                      "AND a.request_hash=r.request_hash AND a.outcome IN ('completed','reconciled')",
                      (store_id, action_id if action_id.startswith("action_") else "action_" + action_id)).fetchone()
