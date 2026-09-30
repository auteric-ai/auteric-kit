from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class ScanStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("SCANNER_DB", str(Path(os.getenv("TMPDIR", "/tmp")) / "ai-shopping-readiness.db"))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._conn() as c:
            c.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS scans(
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT NOT NULL DEFAULT 'Queued',
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scans_updated ON scans(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_scans_adapter_target ON scans(adapter, target_url);
                """
            )

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def create(self, scan_id: str, target_url: str, adapter: str):
        now = time.time()
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT INTO scans(id,status,target_url,adapter,progress,progress_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (scan_id, "queued", target_url, adapter, 0, "Queued", now, now),
            )

    def update_progress(self, scan_id: str, progress: int, message: str, status: str = "running"):
        with self.lock, self._conn() as c:
            c.execute(
                "UPDATE scans SET status=?,progress=?,progress_message=?,updated_at=? WHERE id=?",
                (status, max(0, min(100, int(progress))), message, time.time(), scan_id),
            )

    def complete(self, scan_id: str, result: dict[str, Any]):
        with self.lock, self._conn() as c:
            c.execute(
                "UPDATE scans SET status='completed',progress=100,progress_message='Report ready',error=NULL,result_json=?,updated_at=? WHERE id=?",
                (json.dumps(result), time.time(), scan_id),
            )

    def fail(self, scan_id: str, error: str):
        with self.lock, self._conn() as c:
            c.execute(
                "UPDATE scans SET status='failed',progress_message='Scan failed',error=?,updated_at=? WHERE id=?",
                (error[:2000], time.time(), scan_id),
            )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        if out.get("result_json"):
            try:
                result = json.loads(out["result_json"])
                result["status"] = out["status"]
                result["progress"] = out["progress"]
                result["progress_message"] = out["progress_message"]
                if out.get("error"):
                    result["error"] = out["error"]
                return result
            except json.JSONDecodeError:
                pass
        return {
            "scan_id": out["id"],
            "status": out["status"],
            "target_url": out["target_url"],
            "adapter": out["adapter"],
            "progress": out["progress"],
            "progress_message": out["progress_message"],
            "error": out.get("error"),
        }

    def get(self, scan_id: str) -> dict[str, Any] | None:
        with self.lock, self._conn() as c:
            row = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def metadata(self, scan_id: str) -> dict[str, Any] | None:
        with self.lock, self._conn() as c:
            row = c.execute("SELECT id,adapter,target_url,status FROM scans WHERE id=?", (scan_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 12, adapters: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self.lock, self._conn() as c:
            if adapters:
                placeholders = ",".join("?" for _ in adapters)
                rows = c.execute(
                    f"SELECT * FROM scans WHERE adapter IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                    (*adapters, limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM scans ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def directory(self, limit: int = 25, include_connected: bool = False) -> dict[str, Any]:
        limit = max(1, min(100, int(limit)))
        fetch_limit = max(limit * 8, 100)
        with self.lock, self._conn() as c:
            if include_connected:
                rows = c.execute("SELECT * FROM scans WHERE status='completed' AND result_json IS NOT NULL ORDER BY updated_at DESC LIMIT ?", (fetch_limit,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM scans WHERE status='completed' AND result_json IS NOT NULL AND adapter='generic' ORDER BY updated_at DESC LIMIT ?", (fetch_limit,)).fetchall()
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            record = self._row_to_dict(row)
            target = str(record.get("target_url") or "")
            try:
                from urllib.parse import urlparse
                domain = (urlparse(target).hostname or target).lower()
            except Exception:
                domain = target.lower()
            if not domain or domain in seen:
                continue
            seen.add(domain)
            scores = record.get("scores") or {}
            checks = record.get("checks") or []
            findings = record.get("findings") or []
            score = int(scores.get("overall") if scores.get("overall") is not None else scores.get("security") or 0)
            grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 55 else "F"
            entries.append({
                "rank": len(entries) + 1,
                "scan_id": record.get("scan_id"),
                "domain": domain,
                "target_url": target,
                "platform": record.get("platform") or "unknown",
                "score": score,
                "grade": grade,
                "ucp_score": scores.get("ucp"),
                "web_security": scores.get("web_security"),
                "coverage": scores.get("coverage"),
                "failed_controls": sum(1 for x in checks if x.get("status") == "fail"),
                "warnings": sum(1 for x in checks if x.get("status") == "warning"),
                "critical_high": sum(1 for x in findings if x.get("status") != "pass" and x.get("severity") in {"critical", "high"}),
                "ucp_status": (record.get("observations") or {}).get("ucp_status", "unknown"),
                "completed_at": record.get("completed_at"),
            })
            if len(entries) >= limit:
                break
        return {"sites": entries, "limit": limit, "count": len(entries), "scope": "all" if include_connected else "public"}

    def delete_shopify_shop(self, shop: str) -> int:
        normalized = shop.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
        if not normalized:
            return 0
        candidates = (normalized, f"https://{normalized}", f"http://{normalized}")
        with self.lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM scans WHERE adapter='shopify' AND lower(rtrim(target_url,'/')) IN (lower(?),lower(?),lower(?))",
                candidates,
            )
            return int(cur.rowcount or 0)
