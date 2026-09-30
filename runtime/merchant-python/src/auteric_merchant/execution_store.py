"""MEP/1 execution ledger: idempotency over (installation_id, action_id).

Lifecycle: reserved -> executing -> completed, or executing -> uncertain ->
reconciled (when a write may have happened but the outcome is unknown;
reconciliation happens by action_id per section 5 EXECUTION_UNCERTAIN).

Semantics:
- reserve() is atomic on (installation_id, action_id).
- A second reserve with the same key and a DIFFERENT request_hash raises
  IdempotencyConflict (wire: IDEMPOTENCY_CONFLICT, 409).
- A second reserve with the same key and the same request_hash returns the
  existing record; if it is completed the runtime replays the stored
  response without running the adapter again (section 2.3).

InMemoryExecutionStore is for tests. SQLiteExecutionStore is the reference
implementation for real single-node deployments (sqlite3 stdlib, path
configurable). Multi-node deployments need a shared database-backed
implementation of ExecutionStore (e.g. PostgreSQL with the same unique key);
see django_driver.DjangoExecutionStore for a Django-connection-backed store
that participates in the merchant's own transaction.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

ExecutionStatus = Literal["reserved", "executing", "completed", "uncertain", "reconciled"]


class IdempotencyConflict(Exception):
    """Same (installation, action_id) key arrived with a different payload."""

    def __init__(self, action_id: str) -> None:
        super().__init__(f"conflicting payload for action_id {action_id}")
        self.action_id = action_id


class ExecutionInProgress(Exception):
    """Same key, same payload, but the first attempt is still in flight."""

    def __init__(self, action_id: str) -> None:
        super().__init__(f"action_id {action_id} is still executing")
        self.action_id = action_id


@dataclass(frozen=True)
class ExecutionRecord:
    installation_id: str
    action_id: str
    request_hash: str
    status: ExecutionStatus
    operation: str
    result: dict[str, Any] | None  # validated output payload, when completed
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class ReserveOutcome:
    record: ExecutionRecord
    replay: bool  # True: already completed; runtime returns record.result


@runtime_checkable
class ExecutionStore(Protocol):
    async def reserve(
        self,
        installation_id: str,
        action_id: str,
        request_hash: str,
        operation: str,
    ) -> ReserveOutcome:
        """Atomically reserve the key; see module docstring for semantics."""
        ...

    async def complete(
        self,
        installation_id: str,
        action_id: str,
        result: dict[str, Any],
    ) -> None:
        """Mark the record completed with the validated output payload."""
        ...

    async def mark_uncertain(self, installation_id: str, action_id: str) -> None:
        """Record that the write may have happened but the outcome is unknown."""
        ...

    async def reconcile(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        """Mark an uncertain record reconciled and return it."""
        ...

    async def get(self, installation_id: str, action_id: str) -> ExecutionRecord | None: ...

    async def release(self, installation_id: str, action_id: str) -> None:
        """Drop a non-completed record (failed attempt) so a legitimate retry
        with the same action_id can run. Never removes completed records."""
        ...


class InMemoryExecutionStore:
    """Process-local ledger for tests and single-process development."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ExecutionRecord] = {}
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        installation_id: str,
        action_id: str,
        request_hash: str,
        operation: str,
    ) -> ReserveOutcome:
        async with self._lock:
            key = (installation_id, action_id)
            existing = self._records.get(key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(action_id)
                if existing.status == "completed":
                    return ReserveOutcome(existing, replay=True)
                if existing.status in ("reserved", "executing"):
                    raise ExecutionInProgress(action_id)
                if existing.status in ("uncertain", "reconciled"):
                    raise ExecutionInProgress(action_id)
            now = time.time()
            record = ExecutionRecord(
                installation_id=installation_id,
                action_id=action_id,
                request_hash=request_hash,
                status="executing",
                operation=operation,
                result=None,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._records[key] = record
            return ReserveOutcome(record, replay=False)

    async def complete(self, installation_id: str, action_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            record = self._records[(installation_id, action_id)]
            self._records[(installation_id, action_id)] = ExecutionRecord(
                **{**record.__dict__, "status": "completed", "result": result, "updated_at": time.time()}
            )

    async def mark_uncertain(self, installation_id: str, action_id: str) -> None:
        async with self._lock:
            record = self._records[(installation_id, action_id)]
            self._records[(installation_id, action_id)] = ExecutionRecord(
                **{**record.__dict__, "status": "uncertain", "updated_at": time.time()}
            )

    async def reconcile(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        async with self._lock:
            key = (installation_id, action_id)
            record = self._records.get(key)
            if record is None:
                return None
            if record.status == "uncertain":
                record = ExecutionRecord(
                    **{**record.__dict__, "status": "reconciled", "updated_at": time.time()}
                )
                self._records[key] = record
            return record

    async def get(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        async with self._lock:
            return self._records.get((installation_id, action_id))

    async def release(self, installation_id: str, action_id: str) -> None:
        async with self._lock:
            key = (installation_id, action_id)
            record = self._records.get(key)
            if record is not None and record.status in ("reserved", "executing"):
                del self._records[key]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS auteric_executions (
    installation_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved','executing','completed','uncertain','reconciled')),
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (installation_id, action_id)
)
"""


class SQLiteExecutionStore:
    """Reference ledger for real single-node deployments (sqlite3 stdlib).

    Multi-node deployments must provide a shared-database implementation of
    ExecutionStore instead (the same atomic-reserve semantics rely on the
    PRIMARY KEY uniqueness of (installation_id, action_id)).
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute('CREATE TABLE IF NOT EXISTS execution_nonces(jti TEXT PRIMARY KEY,expires REAL NOT NULL)')
        self._conn.commit()
        self._lock = threading.Lock()

    async def check_and_store(self,jti,ttl_seconds):
        def execute():
            with self._lock, self._conn:
                now=time.time()
                self._conn.execute('DELETE FROM execution_nonces WHERE expires<?',(now,))
                return self._conn.execute('INSERT OR IGNORE INTO execution_nonces VALUES(?,?)',
                    (jti,now+max(60,ttl_seconds))).rowcount==1
        return await asyncio.to_thread(execute)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row | tuple) -> ExecutionRecord:
        import json

        return ExecutionRecord(
            installation_id=row[0],
            action_id=row[1],
            request_hash=row[2],
            operation=row[3],
            status=row[4],
            result=json.loads(row[5]) if row[5] is not None else None,
            created_at=row[6],
            updated_at=row[7],
        )

    def _reserve_sync(
        self, installation_id: str, action_id: str, request_hash: str, operation: str
    ) -> ReserveOutcome:
        with self._lock:
            now = time.time()
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO auteric_executions"
                " (installation_id, action_id, request_hash, operation, status, result_json,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'executing', NULL, ?, ?)",
                (installation_id, action_id, request_hash, operation, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT installation_id, action_id, request_hash, operation, status,"
                " result_json, created_at, updated_at"
                " FROM auteric_executions WHERE installation_id = ? AND action_id = ?",
                (installation_id, action_id),
            ).fetchone()
            assert row is not None
            record = self._row_to_record(row)
            if cursor.rowcount == 1:
                return ReserveOutcome(record, replay=False)
            if record.request_hash != request_hash:
                raise IdempotencyConflict(action_id)
            if record.status == "completed":
                return ReserveOutcome(record, replay=True)
            if record.status in ("reserved", "executing"):
                raise ExecutionInProgress(action_id)
            if record.status in ("uncertain", "reconciled"):
                raise ExecutionInProgress(action_id)
            raise ExecutionInProgress(action_id)

    async def reserve(
        self, installation_id: str, action_id: str, request_hash: str, operation: str
    ) -> ReserveOutcome:
        return await asyncio.to_thread(
            self._reserve_sync, installation_id, action_id, request_hash, operation
        )

    async def complete(self, installation_id: str, action_id: str, result: dict[str, Any]) -> None:
        import json

        def _update() -> None:
            with self._lock:
                self._conn.execute(
                    "UPDATE auteric_executions SET status = 'completed', result_json = ?,"
                    " updated_at = ? WHERE installation_id = ? AND action_id = ?",
                    (json.dumps(result), time.time(), installation_id, action_id),
                )
                self._conn.commit()

        await asyncio.to_thread(_update)

    async def mark_uncertain(self, installation_id: str, action_id: str) -> None:
        def _update() -> None:
            with self._lock:
                self._conn.execute(
                    "UPDATE auteric_executions SET status = 'uncertain', updated_at = ?"
                    " WHERE installation_id = ? AND action_id = ?",
                    (time.time(), installation_id, action_id),
                )
                self._conn.commit()

        await asyncio.to_thread(_update)

    async def reconcile(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        def _run() -> ExecutionRecord | None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT installation_id, action_id, request_hash, operation, status,"
                    " result_json, created_at, updated_at"
                    " FROM auteric_executions WHERE installation_id = ? AND action_id = ?",
                    (installation_id, action_id),
                ).fetchone()
                if row is None:
                    return None
                record = self._row_to_record(row)
                if record.status == "uncertain":
                    self._conn.execute(
                        "UPDATE auteric_executions SET status = 'reconciled', updated_at = ?"
                        " WHERE installation_id = ? AND action_id = ?",
                        (time.time(), installation_id, action_id),
                    )
                    self._conn.commit()
                    record = ExecutionRecord(
                        **{**record.__dict__, "status": "reconciled", "updated_at": time.time()}
                    )
                return record

        return await asyncio.to_thread(_run)

    async def get(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        def _run() -> ExecutionRecord | None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT installation_id, action_id, request_hash, operation, status,"
                    " result_json, created_at, updated_at"
                    " FROM auteric_executions WHERE installation_id = ? AND action_id = ?",
                    (installation_id, action_id),
                ).fetchone()
                return self._row_to_record(row) if row is not None else None

        return await asyncio.to_thread(_run)

    async def release(self, installation_id: str, action_id: str) -> None:
        def _run() -> None:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM auteric_executions"
                    " WHERE installation_id = ? AND action_id = ?"
                    " AND status IN ('reserved','executing')",
                    (installation_id, action_id),
                )
                self._conn.commit()

        await asyncio.to_thread(_run)
