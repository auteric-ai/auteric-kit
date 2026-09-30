"""Django driver: MEP/1 execution endpoint and a transaction-aware ledger.

Wiring (urls.py, BEFORE the SPA catch-all):

    from auteric_merchant.django_driver import make_auteric_view
    urlpatterns = [
        path("api/auteric/v1/<path:full_path>", make_auteric_view(runtime)),
        ...
    ]

Transactions: adapters receive (ctx, input, path_params) and may wrap their
work in django.db.transaction.atomic() themselves. DjangoExecutionStore uses
the merchant's own database connection (django.db.connections) and never
commits explicitly, so ledger writes participate in the merchant's ambient
transaction / autocommit behavior: inside an adapter's transaction.atomic()
block the ledger row commits or rolls back together with the cart update.
For strict ledger+business atomicity, make the adapter open the atomic()
block and have the runtime's store use the same connection (the default
alias unless configured otherwise).

Both an async view (ASGI) and a sync view (WSGI, runtime driven via
async_to_sync) are provided; they behave identically.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Awaitable, Callable

from .execution_store import (
    ExecutionInProgress,
    ExecutionRecord,
    IdempotencyConflict,
    ReserveOutcome,
)
from .runtime import MerchantRuntime, RawRequest, RawResponse

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    installation_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    operation TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    result_json TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (installation_id, action_id)
)
"""


class DjangoExecutionStore:
    """Execution ledger on the merchant's own Django database connection.

    Suitable for single-node and multi-node deployments alike, because the
    uniqueness of (installation_id, action_id) is enforced by the merchant's
    primary database. No explicit commits: statements run under Django's
    autocommit or the caller's transaction.atomic() block, so the ledger
    shares fate with the merchant's business writes.

    Call ensure_schema() once (e.g. from a migration's RunPython or an
    AppConfig.ready hook guarded to run in the web process).
    """

    def __init__(self, *, table: str = "auteric_executions", using: str = "default") -> None:
        if not table.replace("_", "").isalnum():
            raise ValueError("invalid table name")
        self._table = table
        self._using = using
        self._lock = threading.Lock()

    def ensure_schema(self) -> None:
        from django.db import connections

        with connections[self._using].cursor() as cursor:
            cursor.execute(_DDL.format(table=self._table))

    def _connection(self):
        from django.db import connections

        return connections[self._using]

    @staticmethod
    def _row_to_record(row: tuple) -> ExecutionRecord:
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

    _SELECT = (
        "SELECT installation_id, action_id, request_hash, operation, status,"
        " result_json, created_at, updated_at FROM {table}"
        " WHERE installation_id = %s AND action_id = %s"
    )

    async def reserve(
        self, installation_id: str, action_id: str, request_hash: str, operation: str
    ) -> ReserveOutcome:
        from asgiref.sync import sync_to_async

        return await sync_to_async(self._reserve_sync, thread_sensitive=True)(
            installation_id, action_id, request_hash, operation
        )

    def _reserve_sync(
        self, installation_id: str, action_id: str, request_hash: str, operation: str
    ) -> ReserveOutcome:
        now = time.time()
        connection = self._connection()
        with self._lock, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table}"
                " (installation_id, action_id, request_hash, operation, status,"
                "  result_json, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, 'executing', NULL, %s, %s)"
                " ON CONFLICT (installation_id, action_id) DO NOTHING",
                (installation_id, action_id, request_hash, operation, now, now),
            )
            inserted = cursor.rowcount == 1
            cursor.execute(
                self._SELECT.format(table=self._table), (installation_id, action_id)
            )
            record = self._row_to_record(cursor.fetchone())
            if inserted:
                return ReserveOutcome(record, replay=False)
            if record.request_hash != request_hash:
                raise IdempotencyConflict(action_id)
            if record.status == "completed":
                return ReserveOutcome(record, replay=True)
            if record.status in ("reserved", "executing"):
                raise ExecutionInProgress(action_id)
            cursor.execute(
                f"UPDATE {self._table} SET status = 'executing', updated_at = %s"
                " WHERE installation_id = %s AND action_id = %s",
                (now, installation_id, action_id),
            )
            return ReserveOutcome(
                ExecutionRecord(**{**record.__dict__, "status": "executing", "updated_at": now}),
                replay=False,
            )

    async def complete(self, installation_id: str, action_id: str, result: dict[str, Any]) -> None:
        import json

        from asgiref.sync import sync_to_async

        def _update() -> None:
            with self._lock, self._connection().cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._table} SET status = 'completed', result_json = %s,"
                    " updated_at = %s WHERE installation_id = %s AND action_id = %s",
                    (json.dumps(result), time.time(), installation_id, action_id),
                )

        await sync_to_async(_update, thread_sensitive=True)()

    async def mark_uncertain(self, installation_id: str, action_id: str) -> None:
        from asgiref.sync import sync_to_async

        def _update() -> None:
            with self._lock, self._connection().cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._table} SET status = 'uncertain', updated_at = %s"
                    " WHERE installation_id = %s AND action_id = %s",
                    (time.time(), installation_id, action_id),
                )

        await sync_to_async(_update, thread_sensitive=True)()

    async def reconcile(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        from asgiref.sync import sync_to_async

        def _run() -> ExecutionRecord | None:
            with self._lock, self._connection().cursor() as cursor:
                cursor.execute(
                    self._SELECT.format(table=self._table), (installation_id, action_id)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                record = self._row_to_record(row)
                if record.status == "uncertain":
                    cursor.execute(
                        f"UPDATE {self._table} SET status = 'reconciled', updated_at = %s"
                        " WHERE installation_id = %s AND action_id = %s",
                        (time.time(), installation_id, action_id),
                    )
                    record = ExecutionRecord(
                        **{**record.__dict__, "status": "reconciled", "updated_at": time.time()}
                    )
                return record

        return await sync_to_async(_run, thread_sensitive=True)()

    async def get(self, installation_id: str, action_id: str) -> ExecutionRecord | None:
        from asgiref.sync import sync_to_async

        def _run() -> ExecutionRecord | None:
            with self._lock, self._connection().cursor() as cursor:
                cursor.execute(
                    self._SELECT.format(table=self._table), (installation_id, action_id)
                )
                row = cursor.fetchone()
                return self._row_to_record(row) if row is not None else None

        return await sync_to_async(_run, thread_sensitive=True)()

    async def release(self, installation_id: str, action_id: str) -> None:
        from asgiref.sync import sync_to_async

        def _run() -> None:
            with self._lock, self._connection().cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self._table}"
                    " WHERE installation_id = %s AND action_id = %s AND status != 'completed'",
                    (installation_id, action_id),
                )

        await sync_to_async(_run, thread_sensitive=True)()


def _raw_request_from_django(request) -> RawRequest:
    raw_uri = request.META.get("RAW_URI") or request.META.get("REQUEST_URI")
    if raw_uri:
        path, _, query = raw_uri.partition("?")
    else:
        # WSGI fallback: Django already decoded the path; re-encode to UTF-8.
        # For strict raw-path fidelity (MEP/1 section 2.2 step 2), deploy
        # under a server that exposes RAW_URI (e.g. uvicorn/gunicorn ASGI).
        path = request.path_info.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
        query = request.META.get("QUERY_STRING", "")
    headers = {}
    authorization = request.headers.get("authorization")
    if authorization:
        headers["authorization"] = authorization
    return RawRequest(
        method=request.method,
        path=path,
        raw_query_string=query,
        body=request.body,
        headers=headers,
    )


def _to_django_response(raw: RawResponse):
    from django.http import HttpResponse

    response = HttpResponse(raw.body, status=raw.status)
    for name, value in raw.headers.items():
        response[name] = value
    return response


def make_auteric_view(runtime: MerchantRuntime) -> Callable[..., Awaitable[Any]]:
    """Async Django view (ASGI) for the MEP/1 execution endpoint."""

    async def auteric_execute(request, full_path: str = ""):
        raw_request = _raw_request_from_django(request)
        raw_response = await runtime.execute(raw_request)
        return _to_django_response(raw_response)

    return auteric_execute


def make_auteric_view_sync(runtime: MerchantRuntime) -> Callable[..., Any]:
    """Sync Django view (WSGI) with identical behavior; the runtime is
    driven via async_to_sync."""

    from asgiref.sync import async_to_sync

    def auteric_execute(request, full_path: str = ""):
        raw_request = _raw_request_from_django(request)
        raw_response = async_to_sync(runtime.execute)(raw_request)
        return _to_django_response(raw_response)

    return auteric_execute
