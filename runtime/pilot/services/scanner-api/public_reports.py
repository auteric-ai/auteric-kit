"""Allowlisted, durable public observations. Never store raw security evidence here."""
from __future__ import annotations

from readiness import VERSION as READINESS_VERSION, readiness

import ipaddress
import json
import logging
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)


def report_is_readable(row):
    """A blocked observation is retained, but must not replace readable evidence."""
    catalog = (row.get('observations') or {}).get('catalog') or {}
    readiness_data = row.get('readiness') or {}
    return not catalog.get('blocked') and readiness_data.get('discoverable') is not None


def domain_name(value: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    if parsed.username or parsed.password or parsed.port or parsed.scheme not in {"http", "https"}:
        raise ValueError("A public domain is required")
    domain = (parsed.hostname or "").rstrip(".").encode("idna").decode().lower()
    if len(domain) > 253 or "." not in domain or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in domain.split(".")
    ) or domain.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example")):
        raise ValueError("A public domain is required")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("IP addresses cannot have public reports")


def grade(value: int) -> str:
    return "A+" if value >= 95 else "A" if value >= 90 else "B" if value >= 80 else "C" if value >= 70 else "D" if value >= 55 else "F"


def project(record: dict) -> dict | None:
    if record.get("adapter") != "generic" or record.get("status") != "completed":
        return None
    try:
        domain = domain_name(record.get("target_url") or "")
    except (ValueError, UnicodeError):
        return None
    o, s = record.get("observations") or {}, record.get("scores") or {}
    if not isinstance(o.get("http_status"), int):
        return None
    u, a = o.get("ucp_analysis") or {}, o.get("acp_analysis") or {}
    ps = o.get("protocol_summary") or {}
    product_count = int(record.get("products_scanned") or 0)
    # Stores without readable products still need a path to readiness.
    if not record.get("completed_at") or not s:
        return None
    def score(key):
        value = s.get(key)
        return max(0, min(100, int(value))) if isinstance(value, (int, float)) else None
    ai_parts = [score(key) for key in ("discovery", "channel", "checkout")]
    ai = round(sum(ai_parts) / 3) if all(v is not None for v in ai_parts) else None
    catalog, security = score("product_quality"), score("security")
    values = [ai, catalog, security]
    overall = round(sum(values) / 3) if all(v is not None for v in values) else None
    ucp_state = "Detected" if u.get("json_valid") is True else (
        "Partially detected" if o.get("ucp_status") == "invalid" else
        "Unable to verify" if o.get("ucp_http_status") in {None, 401, 403, 429} else "Not detected")
    acp_state = "Detected" if a.get("detected") else (
        "Not yet tested" if not a else "Unable to verify" if a.get("status") == "unable_to_verify" or
        not any(p.get("status") in {200, 204, 404, 405} for p in a.get("probes", [])) else "Not detected")
    transports = [str(t).lower() for t in u.get("transports") or []]
    mcp_state = "Detected" if any(p.get("transport", "").lower() == "mcp" and p.get("reachable") is True for p in u.get("transport_probes") or []) else (
        "Partially detected" if "mcp" in transports else "Not detected" if u else "Not yet tested")
    products = record.get("products") or []
    facts = {key: sum(bool(p.get(key)) if key != "price" else p.get(key) is not None for p in products)
             for key in ("title", "price", "currency", "availability", "variants", "structured_data")}
    # Counts can be recovered from the safe catalog summary for imported historical data.
    bots = {str(k): v for k, v in (o.get("ai_bot_access") or {}).items() if isinstance(v, bool) and k in {"GPTBot", "OAI-SearchBot", "Google-Extended", "Googlebot", "ClaudeBot", "PerplexityBot"}}
    caps = {
        "Discover products": "Detected" if product_count else "Not detected",
        "View product details": "Detected" if facts["title"] else "Unable to verify",
        "Understand price": "Detected" if facts["price"] else "Unable to verify",
        "Understand stock": "Detected" if facts["availability"] else "Unable to verify",
        "Understand variants": "Detected" if facts["variants"] else "Unable to verify",
        "Create cart": "Unable to verify",
        "Reach checkout": "Declared" if u.get("checkout_capability") else "Unable to verify",
        "Complete agentic checkout": "Unable to verify",
        "Use delegated payment": "Unable to verify",
        "Track orders": "Unable to verify",
    }
    platform = str(record.get("platform") or "unknown").lower()
    if platform not in {"shopify", "woocommerce", "magento", "bigcommerce", "wix", "shopware", "prestashop", "custom", "commercetools"}:
        platform = "unknown"
    return {
        "scan_id": str(record["scan_id"]), "domain": domain, "platform": platform,
        "completed_at": str(record["completed_at"]), "score_version": "public-v1", "scan_version": READINESS_VERSION,
        "store_name": str(record.get("store_name") or domain)[:160],
        "readiness": readiness(record),
        "score": overall, "grade": grade(overall) if overall is not None else "Not scored",
        "ai": ai, "catalog": catalog, "security": security,
        "subscores": {key: score(key) for key in ("discovery", "channel", "checkout", "web_security")},
        "protocols": {"ucp": ucp_state, "acp": acp_state, "mcp": mcp_state},
        "acp_source": "Storefront declaration" if a.get("source") == "storefront_markup" else "Public endpoint signal" if a.get("detected") else None,
        "acp_checkout": "Detected" if any(p.get("matched") and p.get("path") == "/checkout_sessions" for p in a.get("probes", [])) else "Unable to verify",
        "products_scanned": product_count, "product_fields": facts,
        "catalog_complete": (o.get("catalog") or {}).get("complete") is True,
        "bots": bots, "robots_status": o.get("robots_status"),
        "payment_handlers": int(u.get("payment_handler_count") or 0),
        "auteric": {
            key: (u.get("auteric_attestation") or {}).get(key)
            for key in ("status", "label", "capability_count")
        },
        "capabilities": caps,
        "ucp_version": str(u.get("version") or "Not detected")[:50],
        "legacy_score": score("overall"),
        "protocol_score": (ps.get("ucp") or {}).get("conformance_score"),
    }


class PublicReports:
    """SQLite read index with immutable safe observations in private S3.

    S3 is the cross-release source of truth; only allowlisted projections leave
    the local scan database. No credentials, products or raw findings are stored.
    """
    def __init__(self, path: str, bucket: str | None = None, client=None):
        self.path, self.bucket = path, bucket or os.getenv("SCANNER_PUBLIC_BUCKET")
        self.lock = threading.RLock()
        self.refreshed = 0.0
        self.client = client
        if self.bucket and self.client is None:
            import boto3
            self.client = boto3.client("s3")
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS public_reports (id TEXT PRIMARY KEY, domain TEXT NOT NULL, scanned TEXT NOT NULL, payload TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS public_domain_scanned ON public_reports(domain, scanned DESC)")

    def _insert(self, row):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO public_reports VALUES(?,?,?,?)", (row["scan_id"], row["domain"], row["completed_at"], json.dumps(row)))

    def publish(self, record):
        row = project(record)
        if row is None:
            return
        self.publish_projection(row)

    def publish_projection(self, row):
        """Internal safe projection writers only; never expose this as a public endpoint."""
        with self.lock:
            if self.bucket:
                self.client.put_object(Bucket=self.bucket, Key=f'observations/{row["scan_id"]}.json',
                                       Body=json.dumps(row).encode(), ContentType="application/json", ServerSideEncryption="AES256")
            self._insert(row)

    def refresh(self):
        with self.lock:
            if not self.bucket or time.monotonic() - self.refreshed < 60:
                return
            with sqlite3.connect(self.path) as c:
                known = {r[0] for r in c.execute("SELECT id FROM public_reports")}
            # Follow every S3 continuation token; never present a capped recent window as the corpus.
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="observations/"):
                for obj in page.get("Contents", []):
                    scan_id = obj["Key"].removeprefix("observations/").removesuffix(".json")
                    if scan_id not in known:
                        row = json.loads(self.client.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read())
                        self._insert(row)
            self.refreshed = time.monotonic()

    def rows(self, domain=None, history=False):
        self.refresh()
        with self.lock, sqlite3.connect(self.path) as c:
            query = "SELECT payload FROM public_reports"
            args = ()
            if domain:
                query += " WHERE domain=?"
                args = (domain,)
            query += " ORDER BY scanned DESC, id DESC"
            rows = [json.loads(r[0]) for r in c.execute(query, args)]
        if history:
            return rows
        latest = {}
        for row in rows:
            current = latest.get(row['domain'])
            if current is None or (not report_is_readable(current) and report_is_readable(row)):
                latest[row['domain']] = row
        return list(latest.values())
