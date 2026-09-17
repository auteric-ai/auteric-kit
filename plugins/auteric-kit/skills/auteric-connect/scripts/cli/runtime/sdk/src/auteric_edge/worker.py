"""Outbound-only queue transport with durable no-replay execution claims."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .mapping import safe_base_url
from .models import READ_OPERATIONS, validate_input, validate_output


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def redact(data):
    """Strip sensitive fields before result transport; never log response bodies."""
    forbidden = {
        "authorization",
        "cookie",
        "set-cookie",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "secret",
        "card_number",
        "cardnumber",
        "cvv",
        "cvc",
        "pan",
        "payment_credentials",
    }
    if isinstance(data, dict):
        return {
            k: (
                "[REDACTED]"
                if k.lower() in forbidden or any(s in k.lower() for s in ("password", "secret", "token"))
                else redact(v)
            )
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    operation: str
    input: dict
    mapping: dict | None = None
    mapping_version: int | str
    expires_at: float | None = None


class EdgeWorker:
    def __init__(
        self,
        connector,
        *,
        api_url,
        store_id,
        token,
        database=".runtime/jobs.db",
        environment="development",
        allow_loopback=False,
        transport=None,
        release_digest=None,
    ):
        self.connector = connector
        self.api_url = safe_base_url(api_url, allow_loopback=allow_loopback)
        if len(token) < 32 or not store_id:
            raise ValueError("Store ID and strong connector token required")
        self.store_id, self.environment = store_id, environment
        if release_digest is not None and (len(release_digest) != 64 or any(c not in "0123456789abcdef" for c in release_digest)):
            raise ValueError("release_digest must be a lowercase SHA-256 artifact digest")
        self.release_digest = release_digest
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("Use persistent local job storage")
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(store TEXT NOT NULL,id TEXT NOT NULL,"
                "fingerprint TEXT NOT NULL,state TEXT NOT NULL,payload TEXT,PRIMARY KEY(store,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipt_outbox(store TEXT NOT NULL,id TEXT NOT NULL,"
                "payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(store,id))"
            )
        self.client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    async def process(self, data):
        job = Job.model_validate(data)
        request = validate_input(job.operation, job.input)
        # UUID revisions identify immutable control-plane drafts; integer versions
        # remain supported for standalone local mappings. Both bind the payload hash.
        if not str(job.mapping_version) or (isinstance(job.mapping_version, int) and job.mapping_version < 1):
            raise ValueError("A nonempty mapping revision is required")
        if (
            isinstance(job.mapping_version, int)
            and job.mapping
            and job.mapping.get("version", job.mapping_version) != job.mapping_version
        ):
            return {
                "result": None,
                "error": {
                    "code": "MAPPING_VERSION_MISMATCH",
                    "message": "Mapping version differs from authorized job",
                    "uncertain": False,
                },
            }
        fingerprint = hashlib.sha256(canonical(job.model_dump(mode="json")).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE store=? AND id=?", (self.store_id, job.id)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    return {
                        "result": None,
                        "error": {
                            "code": "REPLAY_CONFLICT",
                            "message": "Job ID reused with a different exact payload",
                            "uncertain": True,
                        },
                    }
                if existing["payload"]:
                    return json.loads(existing["payload"])
                return {
                    "result": None,
                    "error": {
                        "code": "EXECUTION_UNKNOWN",
                        "message": "Existing execution claim requires reconciliation; not retried",
                        "uncertain": True,
                    },
                }
            if job.expires_at is not None and job.expires_at <= datetime.now(timezone.utc).timestamp():
                return {
                    "result": None,
                    "error": {
                        "code": "EXPIRED",
                        "message": "Authorization expired before local execution",
                        "uncertain": False,
                    },
                }
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,NULL)", (self.store_id, job.id, fingerprint, "executing"))
        try:
            remaining = (job.expires_at - datetime.now(timezone.utc).timestamp()) if job.expires_at else 25
            result = await asyncio.wait_for(
                self.connector.execute(job.operation, request, job.mapping), timeout=max(0.001, remaining)
            )
            payload = {"result": redact(validate_output(job.operation, result)), "error": None}
            state = "executed"
        except BaseException as error:
            # Unknown external write semantics: never infer that an HTTP error or
            # lost response proves a mutation did not happen.
            uncertain = job.operation not in READ_OPERATIONS
            payload = {
                "result": None,
                "error": {
                    "code": "EXECUTION_UNKNOWN" if uncertain else "MERCHANT_REQUEST_FAILED",
                    "message": "Merchant execution did not return a validated result; "
                    "review connector configuration and reconcile writes",
                    "uncertain": uncertain,
                },
            }
            state = "uncertain" if uncertain else "failed"
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET state=?,payload=? WHERE store=? AND id=?",
                (state, canonical(payload), self.store_id, job.id),
            )
            db.execute(
                "INSERT OR IGNORE INTO receipt_outbox(store,id,payload) VALUES(?,?,?)",
                (self.store_id, job.id, canonical(payload)),
            )
        return payload

    async def deliver_receipt(self, job_id, payload):
        reply = await self.client.post(
            f"/api/commerce/edge/jobs/{job_id}/result", json={"store_id": self.store_id, **payload}
        )
        # A permanently refused receipt needs operator reconciliation, not endless
        # head-of-line blocking. Authentication/transient failures remain pending.
        if reply.status_code in {404, 409, 422}:
            receipt_state = "reconciliation_required"
        else:
            reply.raise_for_status()
            receipt_state = "delivered"
        with self.connection() as db:
            db.execute(
                "UPDATE receipt_outbox SET state=? WHERE store=? AND id=?", (receipt_state, self.store_id, job_id)
            )

    async def tick(self):
        with self.connection() as db:
            receipt = db.execute(
                "SELECT id,payload FROM receipt_outbox WHERE store=? AND state='pending' LIMIT 1",
                (self.store_id,),
            ).fetchone()
        if receipt:
            await self.deliver_receipt(receipt["id"], json.loads(receipt["payload"]))
            return True
        response = await self.client.post(
            "/api/commerce/edge/poll",
            json={"store_id": self.store_id, "version": "0.1.0", "environment": self.environment,
                  **({"release_digest": self.release_digest} if self.release_digest else {})},
        )
        response.raise_for_status()
        job = response.json().get("job")
        if not job:
            return False
        payload = await self.process(job)
        # Retry only durable receipts, never merchant writes.
        await self.deliver_receipt(job["id"], payload)
        return True

    async def run(self):
        while True:
            try:
                busy = await self.tick()
            except (httpx.HTTPError, ValueError):
                busy = False
            await asyncio.sleep(0.1 if busy else 2)

    async def close(self):
        await self.client.aclose()
