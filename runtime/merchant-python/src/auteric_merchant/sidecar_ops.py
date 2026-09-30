"""Durable operational state for the deployable merchant sidecar."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class AuditEvent:
    occurred_at: float
    installation_id: str
    action_id: str
    operation: str
    mapping_fingerprint: str
    outcome: str


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(asdict(event))


class SQLiteAuditSink:
    """Append-only local audit storage; payloads and credentials are absent by design."""
    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS auteric_audit ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at REAL NOT NULL, "
            "installation_id TEXT NOT NULL, action_id TEXT NOT NULL, operation TEXT NOT NULL, "
            "mapping_fingerprint TEXT NOT NULL, outcome TEXT NOT NULL)"
        )
        self._connection.commit()
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO auteric_audit (occurred_at, installation_id, action_id, operation, mapping_fingerprint, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.occurred_at, event.installation_id, event.action_id, event.operation,
                 event.mapping_fingerprint, event.outcome),
            )
            self._connection.commit()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT occurred_at, installation_id, action_id, operation, mapping_fingerprint, outcome "
            "FROM auteric_audit ORDER BY sequence DESC LIMIT ?", (max(1, min(limit, 1000)),)
        ).fetchall()
        keys = ("occurred_at", "installation_id", "action_id", "operation", "mapping_fingerprint", "outcome")
        return [dict(zip(keys, row)) for row in rows]


@dataclass(frozen=True)
class LocalPolicySnapshot:
    """A fail-closed cache: it can remove installed permissions, never add them."""
    fingerprint: str
    allowed_operations: tuple[str, ...]
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if not self.fingerprint.startswith("sha256:"):
            raise ValueError("policy snapshot requires a sha256 fingerprint")
        if self.issued_at <= 0 or self.expires_at <= self.issued_at:
            raise ValueError("policy snapshot validity window is invalid")
        if len(set(self.allowed_operations)) != len(self.allowed_operations):
            raise ValueError("policy snapshot operations must be unique")

    def allows(self, operation: str, now: float | None = None) -> bool:
        return (now or time.time()) < self.expires_at and operation in self.allowed_operations


def policy_fingerprint(payload: Mapping[str, Any]) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
