"""Shared PostgreSQL execution/audit/nonce storage for Sidecar replicas.

Separate transactions cannot atomically commit a remote merchant mutation.
Unfinished reservations stay protected until authoritative reconciliation.
"""
from __future__ import annotations

import asyncio
import json
import time

from .execution_store import ExecutionRecord, ReserveOutcome, IdempotencyConflict, ExecutionInProgress


class PostgresExecutionStore:
    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        self._connect = lambda dsn: psycopg.connect(dsn, connect_timeout=5, options='-c statement_timeout=5000')
        with self._connect(dsn) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS auteric_executions (
                installation_id TEXT NOT NULL, action_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                operation TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
                created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(installation_id, action_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS auteric_nonces (
                jti TEXT PRIMARY KEY, expires_at DOUBLE PRECISION NOT NULL)''')

    @staticmethod
    def _record(row):
        return ExecutionRecord(installation_id=row[0], action_id=row[1], request_hash=row[2],
            operation=row[3], status=row[4], result=json.loads(row[5]) if row[5] else None,
            created_at=row[6], updated_at=row[7])

    def _get(self, db, installation_id, action_id):
        return db.execute('SELECT installation_id,action_id,request_hash,operation,status,result_json,created_at,updated_at '
            'FROM auteric_executions WHERE installation_id=%s AND action_id=%s',
            (installation_id, action_id)).fetchone()

    async def reserve(self, installation_id, action_id, request_hash, operation):
        def execute():
            with self._connect(self.dsn) as db:
                now = time.time()
                inserted = db.execute("INSERT INTO auteric_executions VALUES(%s,%s,%s,%s,'executing',NULL,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING action_id", (installation_id,action_id,request_hash,operation,now,now)).fetchone()
                record = self._record(self._get(db, installation_id, action_id))
                if record.request_hash != request_hash or record.operation != operation:
                    raise IdempotencyConflict(action_id)
                if inserted:
                    return ReserveOutcome(record, replay=False)
                if record.status == 'completed':
                    return ReserveOutcome(record, replay=True)
                raise ExecutionInProgress(action_id)
        return await asyncio.to_thread(execute)

    async def complete(self, installation_id, action_id, result):
        await self._update(installation_id,action_id,'completed',json.dumps(result))

    async def mark_uncertain(self, installation_id, action_id):
        await self._update(installation_id,action_id,'uncertain')

    async def _update(self, installation_id, action_id, status, result=None):
        def execute():
            with self._connect(self.dsn) as db:
                db.execute('UPDATE auteric_executions SET status=%s,result_json=COALESCE(%s,result_json),updated_at=%s '
                    'WHERE installation_id=%s AND action_id=%s AND status != %s',
                    (status,result,time.time(),installation_id,action_id,'completed'))
        await asyncio.to_thread(execute)

    async def get(self, installation_id, action_id):
        def execute():
            with self._connect(self.dsn) as db:
                row = self._get(db,installation_id,action_id)
                return self._record(row) if row else None
        return await asyncio.to_thread(execute)

    async def release(self, installation_id, action_id):
        def execute():
            with self._connect(self.dsn) as db:
                db.execute("DELETE FROM auteric_executions WHERE installation_id=%s AND action_id=%s "
                    "AND status IN ('reserved','executing')",(installation_id,action_id))
        await asyncio.to_thread(execute)

    async def reconcile(self, installation_id, action_id):
        record = await self.get(installation_id,action_id)
        if record and record.status == 'uncertain':
            await self._update(installation_id,action_id,'reconciled')
            return await self.get(installation_id,action_id)
        return record

    async def check_and_store(self, jti, ttl_seconds):
        def execute():
            with self._connect(self.dsn) as db:
                now = time.time()
                db.execute('DELETE FROM auteric_nonces WHERE expires_at < %s',(now,))
                return db.execute('INSERT INTO auteric_nonces VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING jti',
                    (jti,now+max(60,ttl_seconds))).fetchone() is not None
        return await asyncio.to_thread(execute)

    def close(self):
        pass  # connections are scoped to each transaction

    async def check_ready(self):
        def execute():
            with self._connect(self.dsn) as db:
                db.execute('SELECT 1 FROM auteric_executions LIMIT 1')
        await asyncio.to_thread(execute)


class PostgresAuditSink:
    def __init__(self, dsn):
        import psycopg
        self.dsn = dsn
        self._connect = lambda dsn: psycopg.connect(dsn, connect_timeout=5, options='-c statement_timeout=5000')
        with self._connect(dsn) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS auteric_sidecar_audit (
                sequence BIGSERIAL PRIMARY KEY, occurred_at DOUBLE PRECISION NOT NULL,
                installation_id TEXT NOT NULL, action_id TEXT NOT NULL, operation TEXT NOT NULL,
                mapping_fingerprint TEXT NOT NULL, outcome TEXT NOT NULL)''')

    def record(self, event):
        with self._connect(self.dsn) as db:
            db.execute('INSERT INTO auteric_sidecar_audit '
                '(occurred_at,installation_id,action_id,operation,mapping_fingerprint,outcome) VALUES(%s,%s,%s,%s,%s,%s)',
                (event.occurred_at,event.installation_id,event.action_id,event.operation,event.mapping_fingerprint,event.outcome))

    async def check_ready(self):
        def execute():
            with self._connect(self.dsn) as db:
                db.execute('SELECT 1 FROM auteric_sidecar_audit LIMIT 1')
        await asyncio.to_thread(execute)
