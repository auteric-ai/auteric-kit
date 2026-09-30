from __future__ import annotations

import asyncio
import copy
import html
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from adapters import GenericWebAdapter, ShopifyAdapter, WooCommerceAdapter
from adapters.generic_web import UnsafeTarget, _is_loopback_host, fetch_public_catalog_page, normalize_target, validate_public_url
from attack_surface import build_attack_surface
from business_risk import build_business_risks
from control_checks import build_checks
from models import AdapterResult, ScanCreateRequest, ScanCreateResponse, ScanResult
from marketing import PAGES, render_marketing_page, sitemap_paths
from onboarding_kit import install_kit_routes
from readiness import VERSION as READINESS_VERSION
from scoring import audit_catalog_products, build_catalog_summary, evaluate
from storage import ScanStore
from public_reports import PublicReports, domain_name
from merchant_claims import MerchantClaims, install_claim_routes
from runtime_readiness import RuntimeReadiness, install_runtime_routes
from public_routes import install_public_routes

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
VERSION = "0.9.0-auteric"

app = FastAPI(title="Auteric Agentic Commerce Scanner", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
store = ScanStore(os.getenv("SCANNER_DB"))
public_reports = PublicReports(store.path)
merchant_claims = MerchantClaims(store.path)
runtime_readiness = RuntimeReadiness(store.path)

ADAPTERS = {
    "generic": GenericWebAdapter(),
    "shopify": ShopifyAdapter(),
    "woocommerce": WooCommerceAdapter(),
}

CONNECTED_ADAPTERS = {"shopify", "woocommerce"}


def _service_key_configured() -> str:
    return os.getenv("SCANNER_SERVICE_KEY", "").strip()


def _require_service_key(provided: str | None) -> None:
    expected = _service_key_configured()
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="Scanner service authentication required")


def _protect_adapter(adapter: str, provided: str | None) -> None:
    if adapter in CONNECTED_ADAPTERS:
        if not _service_key_configured():
            raise HTTPException(status_code=503, detail="Connected scanner authentication is not configured")
        _require_service_key(provided)


def _local_scan_request_allowed(request: Request) -> bool:
    """Loopback targets are a local-development feature, never a public proxy."""
    return bool(request.client and _is_loopback_host(request.client.host or ""))


def _scanner_agent_profile() -> dict[str, Any]:
    """Public, read-only UCP agent identity used only for MCP catalog probes."""
    return {
        "ucp": {
            "version": os.getenv("UCP_CURRENT_VERSION", "2026-08-25"),
            "services": {},
            "capabilities": {},
            "payment_handlers": {},
        },
        "agent": {
            "name": "Auteric Agentic Commerce Scanner",
            "description": "Read-only commerce catalog and UCP conformance scanner.",
        },
    }


def _base_url(request: Request) -> str:
    configured = os.getenv("AUTERIC_PUBLIC_URL", "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def _shopify_install_url() -> str | None:
    """Return the public Shopify install entrypoint only when it is safe to expose."""
    value = os.getenv("AUTERIC_SHOPIFY_INSTALL_URL", "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    return value


def _scanner_shell(
    request: Request,
    *,
    title: str = "AI Shopping & Agentic Commerce Scanner | Auteric",
    description: str = "Scan your store for AI shopping readiness. Check product discoverability, UCP support, checkout readiness and security for AI shopping agents.",
    path: str = "/",
    noindex: bool = False,
) -> HTMLResponse:
    base_url = _base_url(request)
    canonical = f"{base_url}{path}"
    route_meta = "\n".join([
        f'<meta name="description" content="{html.escape(description, quote=True)}" />',
        f'<link rel="canonical" href="{html.escape(canonical, quote=True)}" />',
        f'<meta property="og:title" content="{html.escape(title, quote=True)}" />',
        f'<meta property="og:description" content="{html.escape(description, quote=True)}" />',
        f'<meta property="og:url" content="{html.escape(canonical, quote=True)}" />',
        '<meta property="og:type" content="website" />',
        '<meta property="og:site_name" content="Auteric" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{html.escape(title, quote=True)}" />',
        f'<meta name="twitter:description" content="{html.escape(description, quote=True)}" />',
        '<meta name="robots" content="noindex,nofollow" />' if noindex else '',
    ])
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    markup = markup.replace(
        "<title>AI Shopping &amp; Agentic Commerce Scanner | Auteric</title>",
        f"<title>{html.escape(title)}</title>",
    )
    markup = markup.replace("<!-- AUTERIC_ROUTE_META -->", route_meta)
    return HTMLResponse(markup)


def _playground_shell(request: Request) -> HTMLResponse:
    base_url = _base_url(request)
    markup = (STATIC / "playground.html").read_text(encoding="utf-8")
    markup = markup.replace('<link rel="canonical" href="/playground" />', f'<link rel="canonical" href="{html.escape(base_url + "/playground", quote=True)}" />')
    return HTMLResponse(markup)


def _public_security_report(record: dict[str, Any]) -> dict[str, Any]:
    """Return anonymous readiness data without publishing actionable security detail."""
    if record.get("status") != "completed":
        return record
    public = copy.deepcopy(record)
    findings = public.get("findings") or []
    safe_findings = []
    locked_count = 0
    for finding in findings:
        if finding.get("category") in {"discovery", "product", "channel"} and finding.get("severity") not in {"critical", "high"}:
            finding["evidence"] = []
            safe_findings.append(finding)
        else:
            locked_count += 1
    if locked_count:
        safe_findings.append({
            "id": "security.owner_verification_required",
            "category": "security",
            "severity": "info",
            "status": "unknown",
            "title": "Private security evidence requires ownership verification",
            "summary": "Verify ownership to view detailed security findings and technical evidence.",
            "affected_count": locked_count,
            "confidence": 1.0,
            "evidence": [],
            "recommendation": {
                "title": "Verify this store",
                "why": "Detailed findings could help an attacker when published for a store they do not own.",
                "how": "Use a merchant-authorized integration or ownership verification before requesting the full report.",
                "action_label": "Verify your store",
            },
        })
    observations = public.get("observations") or {}
    ucp = observations.get("ucp_analysis") or {}
    acp = observations.get("acp_analysis") or {}
    local_auteric = observations.get("local_auteric") or {}
    standard_files = observations.get("standard_files") or {}
    public["observations"] = {
        "https": observations.get("https"),
        "http_status": observations.get("http_status"),
        "latency_ms": observations.get("latency_ms"),
        "ucp_http_status": observations.get("ucp_http_status"),
        "ucp_status": observations.get("ucp_status"),
        "ai_bot_access": observations.get("ai_bot_access") or {},
        "catalog": observations.get("catalog") or {},
        "cloudflare": observations.get("cloudflare") or {},
        "protocol_summary": observations.get("protocol_summary") or {},
        "local_auteric": {
            key: local_auteric.get(key)
            for key in ("status", "mode", "connector_online", "agent_access_enabled", "active_operations", "production_verified")
            if key in local_auteric
        },
        "score_explanations": {
            **{key: value for key, value in (observations.get("score_explanations") or {}).items() if key != "security"},
            "security": {key: value for key, value in ((observations.get("score_explanations") or {}).get("security") or {}).items() if key != "deductions"},
        },
        "acp_analysis": {
            "detected": bool(acp.get("detected")),
            "status": acp.get("status") or "not_detected",
            "interface": acp.get("interface"),
            "source": acp.get("source"),
            "probes": [
                {key: row.get(key) for key in ("method", "path", "status", "matched")}
                for row in (acp.get("probes") or [])
                if isinstance(row, dict)
            ],
            "note": acp.get("note"),
        },
        "standard_files": {
            name: {key: value for key, value in (entry or {}).items() if key != "content"}
            for name, entry in standard_files.items()
        },
        "ucp_analysis": {
            "version": ucp.get("version"),
            "current_version": ucp.get("current_version"),
            "version_current": ucp.get("version_current"),
            "json_valid": ucp.get("json_valid"),
            "capability_count": ucp.get("capability_count"),
            "capabilities": ucp.get("capabilities") or [],
            "capability_details": ucp.get("capability_details") or [],
            "transports": ucp.get("transports") or [],
            "catalog_capability": ucp.get("catalog_capability"),
            "checkout_capability": ucp.get("checkout_capability"),
            "payment_handler_count": ucp.get("payment_handler_count"),
            "payment_handlers_declared": ucp.get("payment_handlers_declared"),
            "signing_keys_present": ucp.get("signing_keys_present"),
            "auteric_attestation": ucp.get("auteric_attestation") or {
                "status": "not_detected", "label": "Auteric attestation not detected"
            },
            "authority_valid": ucp.get("authority_valid"),
            "version_mismatches": ucp.get("version_mismatches") or [],
            "broken_reference_count": ucp.get("broken_reference_count"),
            "broken_transport_count": ucp.get("broken_transport_count"),
            "transport_probes": [
                {key: row.get(key) for key in ("transport", "status", "reachable")}
                for row in (ucp.get("transport_probes") or [])
                if isinstance(row, dict)
            ],
            "official_schema_validation": {
                key: (ucp.get("official_schema_validation") or {}).get(key)
                for key in ("engine", "available", "ran", "valid", "version")
            },
            "schema_validation": {
                key: (ucp.get("schema_validation") or {}).get(key)
                for key in ("valid", "warnings")
            },
            "catalog_probe": ucp.get("catalog_probe") or {},
        },
    }
    public["checks"] = [
        {**check, "evidence": None}
        for check in (public.get("checks") or [])
        if check.get("category") in {"ucp", "agent", "catalog"}
    ]
    public["findings"] = safe_findings
    public["attack_surface"] = {
        "public": [],
        "locked": [{
            "title": "Detailed security evidence",
            "summary": "Available after store ownership verification.",
            "status": "not_verified",
        }],
        "public_count": 0,
        "locked_count": max(1, locked_count),
    }
    public["business_risks"] = {
        "headline": "Public readiness scan complete",
        "narrative": "Detailed security and runtime findings require store ownership verification.",
        "items": [],
        "gateway_recommended": True,
    }
    public["security_details_locked"] = True
    public["locked_security_findings"] = locked_count
    return public


def _has_private_report_access(meta: dict[str, Any] | None, provided: str | None) -> bool:
    if not meta or meta.get("adapter") != "generic":
        return True
    expected = _service_key_configured()
    return bool(expected and provided == expected)


def _catalog_page(
    record: dict[str, Any],
    *,
    page: int,
    limit: int,
    query: str | None = None,
    product_filter: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    products = record.get("products") or []
    catalog = (record.get("catalog_summary") or {}).get("catalog") or {}
    catalog_total = int(catalog.get("total_products") or catalog.get("products_discovered") or len(products))
    page = max(1, int(page))
    limit = max(1, min(100, int(limit)))
    needle = (query or "").strip().lower()
    if needle:
        products = [
            product for product in products
            if needle in " ".join(str(product.get(key) or "") for key in ("title", "sku", "brand", "id")).lower()
        ]
    selected_filter = (product_filter or "all").strip().lower()

    def gap_count(product: dict[str, Any]) -> int:
        explicit = product.get("missing_fields") or []
        fields = ((product.get("audit") or {}).get("fields") or {})
        return max(len(explicit), sum(1 for value in fields.values() if value is False))

    if selected_filter == "available":
        products = [
            product for product in products
            if product.get("availability")
            and "out" not in str(product.get("availability")).lower()
            and "unavailable" not in str(product.get("availability")).lower()
        ]
    elif selected_filter == "issues":
        products = [product for product in products if gap_count(product) > 0]
    elif selected_filter == "missing-image":
        products = [product for product in products if not (product.get("images") or [])]
    else:
        selected_filter = "all"

    selected_sort = (sort or "default").strip().lower()
    if selected_sort == "name":
        products = sorted(products, key=lambda product: str(product.get("title") or "").casefold())
    elif selected_sort == "issues":
        products = sorted(products, key=lambda product: (-gap_count(product), str(product.get("title") or "").casefold()))
    else:
        selected_sort = "default"
    total = len(products)
    page_count = max(1, (total + limit - 1) // limit) if total else 0
    page = min(page, page_count) if page_count else 1
    start = (page - 1) * limit
    return {
        "scan_id": record.get("scan_id"),
        "status": record.get("status"),
        "products_scanned": record.get("products_scanned", len(record.get("products") or [])),
        "catalog_summary": record.get("catalog_summary") or {},
        "catalog": catalog,
        "page": page,
        "page_size": limit,
        "page_count": page_count,
        "total": total,
        "catalog_total": catalog_total,
        "returned": len(products[start:start + limit]),
        "has_previous": page > 1,
        "has_next": bool(page_count and page < page_count),
        "query": needle or None,
        "filter": selected_filter,
        "sort": selected_sort,
        "products": products[start:start + limit],
    }





def _hydrate_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Backfill derived report surfaces for scans created by earlier scanner builds.

    Findings/evidence are persisted, but older rows may not contain the newer `checks` or
    catalog summary fields. Rebuild only derivable data; never invent runtime test results.
    """
    if not record or record.get("status") != "completed":
        return record
    try:
        adapter_result = AdapterResult.model_validate({
            "adapter": record.get("adapter") or "generic",
            "platform": record.get("platform") or "generic",
            "store_name": record.get("store_name"),
            "target_url": record.get("target_url") or "https://invalid.example/",
            "products": record.get("products") or [],
            "capabilities": record.get("capabilities") or {},
            "observations": record.get("observations") or {},
            "evidence": [],
        })
        if not record.get("checks"):
            derived = build_checks(adapter_result)
            record["checks"] = [c.model_dump(mode="json") for c in derived]
        if not record.get("catalog_summary"):
            # Product audit metadata did not exist in old reports. Re-run only the local
            # scoring normalization so the catalog UI can explain field-level coverage.
            _, _, _, products, extra = evaluate(adapter_result)
            record["products"] = [p.model_dump(mode="json") for p in products]
            record["catalog_summary"] = extra.get("catalog_summary") or build_catalog_summary(products, adapter_result.observations)
        from models import SecurityCheck
        hydrated_checks = [SecurityCheck.model_validate(x) for x in (record.get("checks") or [])]
        if not record.get("attack_surface"):
            record["attack_surface"] = build_attack_surface(hydrated_checks)
        if not record.get("business_risks"):
            record["business_risks"] = build_business_risks(hydrated_checks, adapter_result.capabilities, adapter_result.observations)
    except Exception as exc:
        record.setdefault("report_hydration", {})["error"] = str(exc)[:240]
    return _upgrade_record(record)


def _upgrade_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rebuild evidence/checks for persisted pre-v0.7 reports when enough raw scan data exists.

    This prevents legacy rows from showing Findings while the test counters are all zero.
    The rebuild uses only evidence already stored in the scan; it never invents network results.
    """
    if not record or record.get("status") != "completed":
        return record
    checks = record.get("checks") or []
    products = record.get("products") or []
    needs_rebuild = record.get("report_schema") != "0.9" or not checks or any("field_checks" not in p for p in products if isinstance(p, dict))
    if not needs_rebuild:
        return record
    try:
        adapter_result = AdapterResult(
            adapter=record.get("adapter") or "generic",
            platform=record.get("platform") or "generic",
            store_name=record.get("store_name"),
            target_url=record.get("target_url") or "",
            products=products,
            capabilities=record.get("capabilities") or {},
            observations=record.get("observations") or {},
            evidence=[],
        )
        scores, findings, rebuilt_checks, rebuilt_products, counts = evaluate(adapter_result)
        record = dict(record)
        record.update(counts)
        record["scores"] = scores.model_dump(mode="json")
        record["checks"] = [c.model_dump(mode="json") for c in rebuilt_checks]
        record["findings"] = [f.model_dump(mode="json") for f in findings]
        record["products"] = [p.model_dump(mode="json") for p in rebuilt_products]
        record["attack_surface"] = build_attack_surface(rebuilt_checks)
        record["business_risks"] = build_business_risks(rebuilt_checks, adapter_result.capabilities, adapter_result.observations)
        record["report_schema"] = "0.9"
    except Exception:
        # Legacy report remains readable; the UI also falls back to findings for counters.
        pass
    return record

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_public_into_connected(connected: AdapterResult, public: AdapterResult) -> AdapterResult:
    connected.observations = {**public.observations, **connected.observations, "public_scan_completed": True}
    for key, capability in public.capabilities.items():
        connected.capabilities.setdefault(key, capability)
    connected.evidence.extend(public.evidence)

    public_by_title = {p.title.strip().lower(): p for p in public.products if p.title}
    for p in connected.products:
        match = public_by_title.get(p.title.strip().lower())
        if match:
            p.structured_data = p.structured_data or match.structured_data
            if not p.url and match.url:
                p.url = match.url
            if not p.images and match.images:
                p.images = match.images
            if not p.availability and match.availability:
                p.availability = match.availability
    return connected


async def run_scan(scan_id: str, request: ScanCreateRequest):
    requested_adapter = request.adapter
    target = (request.target_url or "").strip()
    if not target and requested_adapter not in {"shopify"}:
        store.fail(scan_id, "Store URL is required")
        return

    adapter_name = "generic" if requested_adapter == "auto" else requested_adapter
    adapter = ADAPTERS.get(adapter_name)
    if not adapter:
        store.fail(scan_id, f"Unsupported adapter: {adapter_name}")
        return

    started = now_iso()

    def progress(percent: int, message: str):
        store.update_progress(scan_id, percent, message)

    try:
        progress(5, "Preparing safe scan")
        result = await adapter.scan(target, request.platform_context, progress)

        # Connected platforms are also viewed from the outside. Failure of the public
        # leg is non-fatal; it is recorded as unverified rather than invented.
        if adapter_name in {"shopify", "woocommerce"}:
            try:
                progress(74, "Checking the storefront from an AI shopper's view")
                public = await ADAPTERS["generic"].scan(result.target_url, {}, lambda *_: None)
                result = _merge_public_into_connected(result, public)
            except Exception as exc:
                result.observations["public_scan_completed"] = False
                result.observations["public_scan_error"] = str(exc)[:300]

        progress(84, "Scoring security, UCP trust and discovery evidence")
        scores, findings, checks, products, counts = evaluate(result)
        attack_surface = build_attack_surface(checks)
        business_risks = build_business_risks(checks, result.capabilities, result.observations)
        progress(94, "Prioritizing security findings and fixes")
        payload = ScanResult(
            scan_id=scan_id,
            status="completed",
            target_url=result.target_url,
            adapter=result.adapter,
            platform=result.platform,
            store_name=result.store_name,
            started_at=started,
            completed_at=now_iso(),
            progress=100,
            progress_message="Report ready",
            scores=scores,
            findings=findings,
            checks=checks,
            products=products,
            capabilities=result.capabilities,
            observations=result.observations,
            attack_surface=attack_surface,
            business_risks=business_risks,
            report_schema="0.9",
            **counts,
        ).model_dump(mode="json")
        payload["scan_version"] = READINESS_VERSION
        store.complete(scan_id, payload)
        try:
            public_reports.publish(payload)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Public report persistence failed for scan %s", scan_id)
    except Exception as exc:
        store.fail(scan_id, str(exc))


async def run_scan_with_deadline(scan_id: str, request: ScanCreateRequest):
    """Ensure a background scan always reaches a terminal state."""
    timeout = max(15, min(900, int(os.getenv("SCANNER_SCAN_TIMEOUT_SECONDS", "120"))))
    try:
        await asyncio.wait_for(run_scan(scan_id, request), timeout=timeout)
    except TimeoutError:
        store.fail(scan_id, f"Scan timed out after {timeout} seconds")


@app.get("/")
def index(request: Request):
    return _scanner_shell(request)


@app.get("/playground")
def playground_page(request: Request):
    return _playground_shell(request)


@app.get('/security', include_in_schema=False)
def retired_security_page():
    return RedirectResponse('/', status_code=308)


@app.get("/health")
def health():
    return {"status": "ok", "service": "auteric-agentic-commerce-scanner", "version": VERSION}


@app.get("/.well-known/ucp-agent")
def scanner_agent_profile():
    # Shopify/MCP implementations fetch this URL themselves. Cache it explicitly so
    # edge networks can validate it as a stable public agent profile.
    return JSONResponse(_scanner_agent_profile(), headers={"Cache-Control": "public, max-age=300, must-revalidate"})


@app.get("/api/platforms")
def platforms():
    shopify_install_url = _shopify_install_url()
    return {
        "platforms": [
            {"id": "generic", "name": "Public URL", "mode": "instant", "status": "available"},
            {
                "id": "shopify",
                "name": "Shopify",
                "mode": "connected",
                "status": "install_available" if shopify_install_url else "install_not_configured",
                "install_url": shopify_install_url,
            },
            {"id": "woocommerce", "name": "WooCommerce", "mode": "connected", "status": "connector_ready"},
        ]
    }


async def _normalize_generic_scan_target(target: str) -> str:
    """Preserve a local loopback URL while keeping public scan inputs canonical."""
    normalized = normalize_target(target)
    host = (urlsplit(normalized).hostname or "").lower().rstrip(".")
    if _is_loopback_host(host):
        await validate_public_url(normalized)
        return normalized
    return "https://" + domain_name(target)


@app.post("/api/scans", response_model=ScanCreateResponse, status_code=202)
async def create_scan(payload: ScanCreateRequest, background_tasks: BackgroundTasks, http_request: Request, x_scanner_service_key: str | None = Header(default=None)):
    scan_id = uuid.uuid4().hex
    adapter_name = "generic" if payload.adapter == "auto" else payload.adapter
    _protect_adapter(adapter_name, x_scanner_service_key)
    target = (payload.target_url or "").strip()
    if not target and payload.adapter != "shopify":
        raise HTTPException(status_code=422, detail="Store URL is required")
    if adapter_name == "generic":
        try:
            candidate = normalize_target(target)
            if _is_loopback_host((urlsplit(candidate).hostname or "").lower().rstrip(".")) and not _local_scan_request_allowed(http_request):
                raise UnsafeTarget("Loopback scans require a local Scanner request")
            target = await _normalize_generic_scan_target(target)
        except (ValueError, UnicodeError, UnsafeTarget):
            raise HTTPException(422, "Enter a public store domain, or a loopback URL while running Auteric locally") from None
        payload = payload.model_copy(update={"target_url": target})
    store.create(scan_id, target or "connected-store", adapter_name)
    background_tasks.add_task(run_scan_with_deadline, scan_id, payload)
    return ScanCreateResponse(scan_id=scan_id, status="queued")


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str, include_products: bool = True, x_scanner_service_key: str | None = Header(default=None)):
    meta = store.metadata(scan_id)
    if meta:
        _protect_adapter(meta["adapter"], x_scanner_service_key)
    record = _hydrate_record(store.get(scan_id))
    if not record:
        raise HTTPException(status_code=404, detail="Scan not found")
    if not _has_private_report_access(meta, x_scanner_service_key):
        record = _public_security_report(record)
    if record.get("status") == "completed" and not include_products:
        # The Catalog endpoint is paginated. Keep this opt-in for older API clients
        # while the browser report avoids transferring thousands of product cards.
        record = {**record, "products": []}
    return record


@app.get("/api/scans/{scan_id}/checks")
def get_scan_checks(scan_id: str, x_scanner_service_key: str | None = Header(default=None)):
    meta = store.metadata(scan_id)
    if meta:
        _protect_adapter(meta["adapter"], x_scanner_service_key)
    record = _hydrate_record(store.get(scan_id))
    if not record:
        raise HTTPException(status_code=404, detail="Scan not found")
    if not _has_private_report_access(meta, x_scanner_service_key):
        return {"scan_id": scan_id, "status": record.get("status"), "checks": [], "security_details_locked": True}
    if record.get("status") != "completed":
        return {"scan_id": scan_id, "status": record.get("status"), "checks": []}
    return {"scan_id": scan_id, "status": "completed", "checks": record.get("checks") or [], "scores": record.get("scores") or {}}


@app.get("/api/scans/{scan_id}/attacks")
def get_scan_attacks(scan_id: str, x_scanner_service_key: str | None = Header(default=None)):
    meta = store.metadata(scan_id)
    if meta:
        _protect_adapter(meta["adapter"], x_scanner_service_key)
    record = _hydrate_record(store.get(scan_id))
    if not record:
        raise HTTPException(status_code=404, detail="Scan not found")
    if not _has_private_report_access(meta, x_scanner_service_key):
        return {
            "scan_id": scan_id,
            "status": record.get("status"),
            "security_details_locked": True,
            "attack_surface": _public_security_report(record).get("attack_surface"),
        }
    if record.get("status") != "completed":
        return {"scan_id": scan_id, "status": record.get("status"), "attack_surface": {"public": [], "locked": []}}
    return {"scan_id": scan_id, "status": "completed", "attack_surface": record.get("attack_surface") or {"public": [], "locked": []}}


@app.get("/api/scans/{scan_id}/business-risks")
def get_scan_business_risks(scan_id: str, x_scanner_service_key: str | None = Header(default=None)):
    meta = store.metadata(scan_id)
    if meta:
        _protect_adapter(meta["adapter"], x_scanner_service_key)
    record = _hydrate_record(store.get(scan_id))
    if not record:
        raise HTTPException(status_code=404, detail="Scan not found")
    if not _has_private_report_access(meta, x_scanner_service_key):
        return {
            "scan_id": scan_id,
            "status": record.get("status"),
            "security_details_locked": True,
            "business_risks": _public_security_report(record).get("business_risks"),
        }
    if record.get("status") != "completed":
        return {"scan_id": scan_id, "status": record.get("status"), "business_risks": {"items": []}}
    return {"scan_id": scan_id, "status": "completed", "business_risks": record.get("business_risks") or {"items": []}}


@app.get("/api/scans/{scan_id}/catalog")
async def get_scan_catalog(
    scan_id: str,
    page: int = 1,
    limit: int = 50,
    q: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
    x_scanner_service_key: str | None = Header(default=None),
):
    meta = store.metadata(scan_id)
    if meta:
        _protect_adapter(meta["adapter"], x_scanner_service_key)
    record = _hydrate_record(store.get(scan_id))
    if not record:
        raise HTTPException(status_code=404, detail="Scan not found")
    if record.get("status") != "completed":
        return {"scan_id": scan_id, "status": record.get("status"), "products": []}
    catalog = (record.get("catalog_summary") or {}).get("catalog") or {}
    lazy = bool(catalog.get("lazy_pagination")) and record.get("platform") in {"shopify", "woocommerce"}
    if lazy:
        page_size = max(1, min(50, int(limit)))
        catalog_total = int(catalog.get("total_products") or catalog.get("products_discovered") or 0)
        page_count = max(1, (catalog_total + page_size - 1) // page_size) if catalog_total else 0
        page = max(1, min(int(page), page_count or 1))
        if page == 1:
            batch = record.get("products") or []
        else:
            try:
                fetched = await fetch_public_catalog_page(
                    record.get("target_url") or "",
                    str(record.get("platform") or ""),
                    page,
                    page_size,
                    catalog.get("currency_hint"),
                )
            except ValueError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            batch = [product.model_dump(mode="json") for product in audit_catalog_products(fetched)]

        # Shopify sitemaps can contain a stale or SEO-only URL that the public
        # products endpoint no longer returns. A short terminal page gives us an
        # authoritative browsable total without downloading every prior page.
        if len(batch) < page_size:
            catalog_total = min(catalog_total, ((page - 1) * page_size) + len(batch))
            page_count = page if catalog_total else 0

        selected = _catalog_page(
            {"scan_id": scan_id, "status": "completed", "products": batch, "catalog_summary": record.get("catalog_summary") or {}},
            page=1,
            limit=page_size,
            query=q,
            product_filter=filter,
            sort=sort,
        )
        visible = selected.get("products") or []
        return {
            "scan_id": scan_id,
            "status": "completed",
            "products_scanned": len(batch),
            "catalog_summary": record.get("catalog_summary") or {},
            "catalog": {**catalog, "current_page_scanned": len(batch), "browsable_total": catalog_total},
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
            "total": catalog_total,
            "catalog_total": catalog_total,
            "returned": len(visible),
            "page_products": len(batch),
            "matching_on_page": selected.get("total", len(visible)),
            "has_previous": page > 1,
            "has_next": bool(page_count and page < page_count),
            "query": selected.get("query"),
            "filter": selected.get("filter"),
            "sort": selected.get("sort"),
            "pagination_mode": "on_demand",
            "products": visible,
        }
    return _catalog_page(record, page=page, limit=limit, query=q, product_filter=filter, sort=sort)


@app.get("/api/scans")
def list_scans(limit: int = 12, x_scanner_service_key: str | None = Header(default=None)):
    expected = _service_key_configured()
    if not expected or x_scanner_service_key != expected:
        scans = []
        for record in store.recent(limit, adapters=("generic",)):
            public = _public_security_report(record)
            public["products"] = []
            scans.append(public)
        return {"scans": scans}
    return {"scans": store.recent(limit)}


@app.get("/api/directory")
def directory(limit: int = 25, x_scanner_service_key: str | None = Header(default=None)):
    expected = _service_key_configured()
    include_connected = bool(expected and x_scanner_service_key == expected)
    return store.directory(limit=limit, include_connected=include_connected)


@app.post("/internal/shopify/redact")
def redact_shopify(payload: dict[str, Any], x_scanner_service_key: str | None = Header(default=None)):
    if not _service_key_configured():
        raise HTTPException(status_code=503, detail="Service authentication is not configured")
    _require_service_key(x_scanner_service_key)
    shop = str(payload.get("shop") or "").strip()
    if not shop:
        raise HTTPException(status_code=422, detail="shop is required")
    deleted = store.delete_shopify_shop(shop)
    return {"status": "ok", "deleted_scans": deleted}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    base_url = _base_url(request)
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /internal/",
        "Disallow: /admin/",
        f"Sitemap: {base_url}/sitemap.xml",
        "",
    ])


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt(request: Request):
    base_url = _base_url(request)
    return f"""# Auteric

> Auteric helps merchants understand whether AI shopping agents can discover, interpret and safely reach the purchase path on their store.

## Core product
- [{base_url}/]({base_url}/): Free, non-destructive agentic commerce readiness scan
- [{base_url}/ai-shopping-readiness]({base_url}/ai-shopping-readiness): AI shopping readiness explained
- [{base_url}/ucp-validator]({base_url}/ucp-validator): Current UCP checks and limitations
- [{base_url}/playground]({base_url}/playground): Read-only UCP, transport and catalog playground
- [{base_url}/agentic-commerce-security]({base_url}/agentic-commerce-security): Agentic commerce security model

The public scanner makes no purchase, requires no credentials and does not publish detailed security evidence without ownership verification.
"""


install_claim_routes(app, merchant_claims, _base_url, _scanner_shell)
install_runtime_routes(app, runtime_readiness, merchant_claims, public_reports)
install_kit_routes(app)
install_public_routes(app, public_reports, store, _scanner_shell, _base_url, merchant_claims, runtime_readiness)


@app.get("/{marketing_slug}")
def marketing_page(marketing_slug: str, request: Request):
    page = PAGES.get(marketing_slug)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return HTMLResponse(render_marketing_page(page, _base_url(request)))
