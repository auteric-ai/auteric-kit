"""Replaceable credential persistence boundary for the control plane.

The SQLite implementation is development/pilot storage. A production deployment
can replace this object with AWS Secrets Manager, Vault or another backend without
changing gateway or onboarding semantics.
"""

from __future__ import annotations

import time
from typing import Protocol

from .storage import digest


class CredentialVault(Protocol):
    def resolve(self, token: str, kind: str, store_id: str | None = None) -> dict | None: ...

    def rotate(self, store_id: str, kind: str, token: str) -> None: ...

    def is_active(self, credential_id: str, kind: str, store_id: str) -> bool: ...


class SQLiteCredentialVault:
    def __init__(self, store):
        self.store = store

    def resolve(self, token: str, kind: str, store_id: str | None = None) -> dict | None:
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM credentials WHERE hash=? AND kind=? AND revoked IS NULL",
                (digest(token), kind),
            ).fetchone()
        if not row or (store_id is not None and row["store"] != store_id):
            return None
        return dict(row)

    def rotate(self, store_id: str, kind: str, token: str) -> None:
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE credentials SET revoked=? WHERE store=? AND kind=? AND revoked IS NULL",
                (time.time(), store_id, kind),
            )
            db.execute(
                "INSERT INTO credentials(hash,store,kind,created,revoked) VALUES(?,?,?,?,NULL)",
                (digest(token), store_id, kind, time.time()),
            )

    def is_active(self, credential_id: str, kind: str, store_id: str) -> bool:
        with self.store.db() as db:
            return db.execute(
                "SELECT 1 FROM credentials WHERE hash=? AND kind=? AND store=? AND revoked IS NULL",
                (credential_id, kind, store_id),
            ).fetchone() is not None
