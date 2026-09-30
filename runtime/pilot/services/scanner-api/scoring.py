from __future__ import annotations

from collections import Counter
from typing import Any

from control_checks import build_checks
from models import AdapterResult, Evidence, Finding, NormalizedProduct, Recommendation, ScoreBreakdown, SecurityCheck
from ucp_checks import build_protocol_summary


SEVERITY_WEIGHT = {"critical": 5.0, "high": 3.0, "medium": 2.0, "low": 1.0, "info": 0.5}
STATUS_VALUE = {"pass": 1.0, "warning": 0.5, "fail": 0.0}
PRODUCT_WEIGHTS = {"description": 15, "price": 15, "image": 10, "brand": 5, "sku": 25, "availability": 15, "category": 5, "canonical URL": 10}

PRODUCT_EXPOSURES = {
    "description": "Agents have less semantic context and may match or summarize the product incorrectly.",
    "price": "Agents cannot reliably quote or compare the current commercial price.",
    "image": "Users and agents have less visual evidence to identify and compare the product.",
    "brand": "Product identity can be ambiguous across similar catalog items.",
    "sku": "Agent-to-checkout item mapping is weaker without a stable merchant SKU.",
    "GTIN/barcode": "Cross-catalog identity and deduplication are weaker without a global identifier.",
    "availability": "Agents may recommend or attempt to buy items without verified stock state.",
    "category": "Product matching and policy/category reasoning are less precise.",
    "canonical URL": "Agents have a weaker canonical identity/link target for the product.",
    "variant details": "An agent may select the wrong variant or use incomplete variant pricing/availability.",
}


def _pct(num: int, den: int, default: int = 0) -> int:
    if den <= 0:
        return default
    return max(0, min(100, round((num / den) * 100)))


def _product_quality(product: NormalizedProduct) -> tuple[int, list[str], list[str], list[dict[str, Any]]]:
    checks = {
        "description": bool(product.description and len(product.description.strip()) >= 40),
        "price": product.price is not None,
        "image": bool(product.images),
        "brand": bool(product.brand),
        "sku": bool(product.sku),
        "availability": bool(product.availability),
        "category": bool(product.categories),
        "canonical URL": bool(product.url),
    }
    score = sum(PRODUCT_WEIGHTS[name] for name, ok in checks.items() if ok)
    missing = [name for name, ok in checks.items() if not ok]
    passed = [name for name, ok in checks.items() if ok]
    labels = {
        "description": "Description", "price": "Price", "image": "Image", "brand": "Brand",
        "sku": "SKU", "GTIN/barcode": "GTIN / barcode", "availability": "Availability",
        "category": "Category", "canonical URL": "Product URL", "variant details": "Variant details",
    }
    field_checks = [{
        "field": name, "label": labels.get(name, name), "status": "pass" if ok else ("not_checked" if product.evidence_scope == "storefront_card" else "not_found"),
        "weight": PRODUCT_WEIGHTS[name], "lost_points": 0 if ok else PRODUCT_WEIGHTS[name], "required": name == "sku",
        "meaning": ("Present in the captured source; accuracy is not independently certified." if ok else "Not verified in the inspected source; other interfaces may contain this field."),
    } for name, ok in checks.items()]
    return score, missing, passed, field_checks




def _product_audit(product: NormalizedProduct) -> dict[str, Any]:
    fields = {
        "description": bool(product.description and len(product.description.strip()) >= 40),
        "price": product.price is not None,
        "image": bool(product.images),
        "brand": bool(product.brand),
        "sku": bool(product.sku),
        "gtin": bool(product.gtin),
        "availability": bool(product.availability),
        "category": bool(product.categories),
        "canonical_url": bool(product.url),
        "variants": bool(product.variants),
    }
    verified = sum(1 for k, v in fields.items() if v and k not in {"variants", "gtin"})
    total = len(fields) - 2
    if verified >= 8:
        label = "Strong product evidence"
    elif verified >= 5:
        label = "Partial product evidence"
    else:
        label = "Limited product evidence"
    return {
        "verified_fields": verified,
        "total_fields": total,
        "label": label,
        "fields": fields,
        "field_states": {k: "present" if v else ("not_checked" if product.evidence_scope == "storefront_card" or k == "variants" else "not_found") for k, v in fields.items()},
        "evidence_scope": product.evidence_scope,
        "evidence_urls": product.evidence_urls,
        "score_scope": "Eight weighted evidence checks. SKU is required (25 points). GTIN and variants are supplementary, not scored without applicability evidence.",
        "required_fields_verified": bool(product.sku),
        "score_breakdown": _product_quality(product)[3],
        "source": product.source,
        "source_confidence": product.source_confidence,
        "variant_count": len(product.variants),
    }


def build_catalog_summary(products: list[NormalizedProduct], observations: dict[str, Any] | None = None) -> dict[str, Any]:
    observations = observations or {}
    total = len(products)
    field_names = ["description","price","image","brand","sku","gtin","availability","category","canonical_url","variants"]
    coverage = {}
    for field in field_names:
        count = sum(1 for p in products if bool((p.audit or {}).get("fields", {}).get(field)))
        unchecked = sum(1 for p in products if (p.audit or {}).get("field_states", {}).get(field) == "not_checked")
        coverage[field] = {"count": count, "total": total, "percent": _pct(count, total) if total else 0, "not_checked": unchecked}
    common_gaps = [
        {"field": field, **stats}
        for field, stats in coverage.items()
        if total and stats["percent"] < 60 and field not in {"variants", "gtin"}
    ]
    common_gaps.sort(key=lambda x: (x["percent"], x["field"]))
    ucp_catalog = ((observations.get("ucp_analysis") or {}).get("catalog_probe") or {})
    sources = sorted({p.source for p in products})
    return {
        "products": total,
        "catalog": observations.get("catalog") or {
            "source": "scan_result",
            "products_discovered": total,
            "pages_fetched": 1,
            "complete": True,
        },
        "with_images": coverage.get("image", {}).get("count", 0),
        "with_price": coverage.get("price", {}).get("count", 0),
        "with_availability": coverage.get("availability", {}).get("count", 0),
        "with_variants": coverage.get("variants", {}).get("count", 0),
        "coverage": coverage,
        "common_gaps": common_gaps,
        "sources": sources,
        "ucp_catalog_probe": ucp_catalog,
    }

def _ev(kind: str, value: Any, *, source="inferred", product_id: str | None = None, field: str | None = None, url: str | None = None) -> Evidence:
    return Evidence(type=kind, source=source, value=value, product_id=product_id, field=field, url=url)


def _friendly_finding_copy(check: SecurityCheck) -> tuple[str, str]:
    evidence = check.evidence if isinstance(check.evidence, dict) else {}
    if check.id == "ucp.keys" and check.status in {"fail", "warning"}:
        return ("Merchant signing keys are not published in the UCP profile", "We fetched the public UCP profile and did not find usable verification keys in its root key material. This does not mean HTTPS is broken or the store is compromised; it means agents using this UCP discovery path cannot independently verify merchant-signed material from the published profile.")
    if check.id == "ucp.references" and check.status in {"fail", "warning"}:
        broken = sum(1 for row in (check.evidence or []) if isinstance(row, dict) and not row.get("ok")) if isinstance(check.evidence, list) else 0
        return (f"{broken or 'One or more'} UCP references failed validation", "The manifest was found, but at least one declared spec/schema/profile reference did not resolve cleanly or failed trust/identity validation. Agents may fail when they follow that declaration.")
    if check.id == "web.cookie_flags" and check.status in {"fail", "warning"}:
        affected = len(evidence.get("sensitive_cookie_issues") or evidence.get("sensitive_without_secure_httponly") or [])
        return ("Sensitive-looking cookies need stronger flags", f"The storefront returned {affected or 'one or more'} session/auth/cart-style cookies without the full Secure + HttpOnly hardening expected by this scanner. Review the raw cookie evidence before changing application behavior.")
    if check.id == "web.hsts" and check.status in {"fail", "warning"}:
        return ("HSTS is missing or below the recommended threshold", "HTTPS is separate from HSTS. This check means the Strict-Transport-Security header was absent or weaker than the scanner threshold; it does not mean TLS itself failed.")
    if check.id == "catalog.ucp_search" and check.status in {"fail", "warning"}:
        return ("Declared UCP catalog search did not return usable products", "The store advertises catalog discovery, but the scanner could not confirm a usable product result from the read-only live catalog probe.")
    return (check.title, check.result if check.status == "pass" else check.exposure)


def _finding_from_check(check: SecurityCheck, base_url: str) -> Finding:
    category = "security"
    if check.category == "ucp":
        category = "ucp"
    elif check.category in {"agent", "transaction"}:
        category = "trust"
    elif check.category == "catalog":
        category = "product"
    rec = None
    if check.recommendation:
        rec = Recommendation(
            title=check.title,
            why=check.exposure,
            how=check.recommendation,
        )
    title, summary = _friendly_finding_copy(check)
    return Finding(
        id=check.id,
        category=category,
        severity=check.severity,
        status=check.status,
        title=title,
        summary=summary,
        confidence=check.confidence,
        evidence=[_ev(check.id, check.evidence, source=check.source, url=base_url)],
        recommendation=rec,
    )


def _score_checks(checks: list[SecurityCheck], categories: set[str]) -> tuple[int, int, int]:
    relevant = [c for c in checks if c.category in categories]
    known = [c for c in relevant if c.status in STATUS_VALUE]
    if not known:
        return 0, 0, len(relevant)
    earned = 0.0
    possible = 0.0
    for c in known:
        weight = SEVERITY_WEIGHT.get(c.severity, 1.0)
        possible += weight
        earned += weight * STATUS_VALUE[c.status]
    return round((earned / possible) * 100) if possible else 0, len(known), len(relevant)



def _score_check_ids(checks: list[SecurityCheck], ids: set[str]) -> tuple[int, int, int]:
    relevant = [c for c in checks if c.id in ids]
    known = [c for c in relevant if c.status in STATUS_VALUE]
    if not known:
        return 0, 0, len(relevant)
    earned = 0.0
    possible = 0.0
    for c in known:
        weight = SEVERITY_WEIGHT.get(c.severity, 1.0)
        possible += weight
        earned += weight * STATUS_VALUE[c.status]
    return round((earned / possible) * 100) if possible else 0, len(known), len(relevant)

def audit_catalog_products(products: list[NormalizedProduct]) -> list[NormalizedProduct]:
    for product in products:
        score, missing, passed, field_checks = _product_quality(product)
        product.ai_product_score = score
        product.missing_fields = missing
        product.passed_checks = passed
        product.exposure = [PRODUCT_EXPOSURES[name] for name in missing if name in PRODUCT_EXPOSURES]
        product.field_checks = field_checks
        product.audit = _product_audit(product)
    return products


def evaluate(adapter_result: AdapterResult) -> tuple[ScoreBreakdown, list[Finding], list[SecurityCheck], list[NormalizedProduct], dict[str, int]]:
    products = audit_catalog_products(adapter_result.products)

    total = len(products)
    ready = sum(1 for p in products if (p.ai_product_score or 0) >= 80 and p.published)
    needs = sum(1 for p in products if 55 <= (p.ai_product_score or 0) < 80 or not p.published)
    high_risk = sum(1 for p in products if (p.ai_product_score or 0) < 55)

    checks = build_checks(adapter_result)
    web_score, web_known, web_total = _score_checks(checks, {"transport", "web"})
    protocol_summary = build_protocol_summary(
        adapter_result.observations.get("ucp_analysis") or {},
        adapter_result.observations.get("acp_analysis") or {},
        transaction_tests=adapter_result.observations.get("transaction_tests") or [],
    )
    adapter_result.observations["protocol_summary"] = protocol_summary
    ucp_score = int((protocol_summary.get("ucp") or {}).get("conformance_score") or 0)
    agent_score_raw, agent_known, agent_total = _score_check_ids(checks, {
        "ucp.keys", "ucp.references", "ucp.authority",
        "agent.https_references", "agent.transport", "agent.ucp_cors",
    })
    transaction_score_raw, tx_known, tx_total = _score_checks(checks, {"transaction"})
    agent_score: int | None = agent_score_raw if agent_known else None
    transaction_score: int | None = transaction_score_raw if tx_known else None

    # Discovery/catalog is supporting context and is intentionally outside the Security score.
    published_count = sum(1 for p in products if p.published)
    with_price = sum(1 for p in products if p.price is not None)
    with_image = sum(1 for p in products if p.images)
    discovery_parts = [_pct(published_count, total), _pct(with_price, total), _pct(with_image, total)] if total else [0]
    bots = adapter_result.observations.get("ai_bot_access") or {}
    if bots:
        discovery_parts.append(_pct(sum(1 for v in bots.values() if v), len(bots)))
    standard = adapter_result.observations.get("standard_files") or {}
    if (standard.get("sitemap") or {}).get("present"):
        discovery_parts.append(100)
    discovery = round(sum(discovery_parts) / len(discovery_parts)) if discovery_parts else 0
    product_quality = round(sum((p.ai_product_score or 0) for p in products) / total) if total else 0

    checkout_cap = adapter_result.capabilities.get("checkout")
    checkout_score = 85 if checkout_cap and checkout_cap.supported else 35
    public_api = adapter_result.capabilities.get("public_product_api")
    ucp_analysis = adapter_result.observations.get("ucp_analysis") or {}
    ucp_public = adapter_result.observations.get("ucp_status") == "verified"
    catalog_probe_ok = bool((ucp_analysis.get("catalog_probe") or {}).get("ok"))
    bots = adapter_result.observations.get("ai_bot_access") or {}
    channel = min(100, sum((
        40 if ucp_public else 0,
        20 if ucp_analysis.get("capability_count") else 0,
        20 if ucp_analysis.get("transports") else 0,
        10 if catalog_probe_ok or (public_api and public_api.supported) else 0,
        10 if bots and all(bool(value) for value in bots.values()) else 0,
    )))

    # Security is evidence-weighted. Unknown categories are excluded instead of being silently passed or failed.
    components: list[tuple[int, float]] = []
    if web_known:
        components.append((web_score, 0.65))
    if agent_known and agent_score is not None:
        components.append((agent_score, 0.20))
    if tx_known and transaction_score is not None:
        components.append((transaction_score, 0.15))
    if components:
        total_weight = sum(weight for _, weight in components)
        security = round(sum(score * weight for score, weight in components) / total_weight)
    else:
        security = 0

    coverage_known = sum(1 for c in checks if c.status in STATUS_VALUE)
    coverage_total = len(checks)
    coverage = _pct(coverage_known, coverage_total)
    transaction_coverage = _pct(tx_known, tx_total)

    overall = round(security * 0.55 + ucp_score * 0.25 + discovery * 0.10 + product_quality * 0.10)
    adapter_result.observations["score_explanations"] = {
        "version": "evidence-weighted-v2",
        "overall": {"scope": "Composite public evidence index, not a checkout or security certification", "components": [
            {"name": name, "score": value, "weight": weight, "lost_points": round((100-value)*weight, 3)}
            for name, value, weight in [("Security", security, .55), ("UCP conformance", ucp_score, .25), ("AI visibility signals", discovery, .10), ("Catalog", product_quality, .10)]
        ]},
        "catalog": {"weights": PRODUCT_WEIGHTS, "required": ["sku"], "supplementary": ["gtin", "variants"], "scope": "Captured source evidence; unverified fields earn no points, but do not prove merchant data is absent."},
        "security": {"scope": "Known public checks only; unknown runtime/transaction checks are excluded", "components": [
            {"name": name, "score": score, "weight": weight / sum(w for _, w in components)}
            for name, score, weight, known in [("Web/TLS", web_score, .65, web_known), ("Agent trust", agent_score, .20, agent_known), ("Transaction", transaction_score, .15, tx_known)] if known
        ], "deductions": [
            {"id": c.id, "title": c.title, "status": c.status, "category": c.category, "severity": c.severity, "reason": c.result, "recommendation": c.recommendation}
            for c in checks if c.status in {"fail", "warning"} and (c.category in {"web", "transport", "transaction"} or c.id in {"ucp.keys", "ucp.references", "ucp.authority", "agent.https_references", "agent.transport", "agent.ucp_cors"})
        ]},
    }
    security_groups = [
        ([c for c in checks if c.category in {"web", "transport"} and c.status in STATUS_VALUE], .65),
        ([c for c in checks if c.id in {"ucp.keys", "ucp.references", "ucp.authority", "agent.https_references", "agent.transport", "agent.ucp_cors"} and c.status in STATUS_VALUE], .20),
        ([c for c in checks if c.category == "transaction" and c.status in STATUS_VALUE], .15),
    ]
    active_weight = sum(weight for group, weight in security_groups if group)
    for deduction in adapter_result.observations["score_explanations"]["security"]["deductions"]:
        for group, weight in security_groups:
            check = next((c for c in group if c.id == deduction["id"]), None)
            if check:
                denominator = sum(SEVERITY_WEIGHT.get(c.severity, 1) for c in group)
                deduction["lost_points_before_rounding"] = round(100 * weight / active_weight * SEVERITY_WEIGHT.get(check.severity, 1) / denominator * (1-STATUS_VALUE[check.status]), 3)
    revenue = round(product_quality * 0.55 + discovery * 0.45)
    scores = ScoreBreakdown(
        discovery=discovery,
        product_quality=product_quality,
        checkout=checkout_score,
        channel=channel,
        ucp=ucp_score,
        protocol_conformance=ucp_score,
        web_security=web_score,
        agent_security=agent_score,
        transaction_security=transaction_score,
        security=max(0, min(100, security)),
        overall=max(0, min(100, overall)),
        revenue=max(0, min(100, revenue)),
        coverage=coverage,
        transaction_coverage=transaction_coverage,
    )

    findings: list[Finding] = []
    for check in checks:
        if check.status in {"fail", "warning"}:
            findings.append(_finding_from_check(check, adapter_result.target_url))
        elif check.status == "unknown" and check.category != "transaction" and check.severity in {"critical", "high"}:
            findings.append(_finding_from_check(check, adapter_result.target_url))

    if tx_known == 0:
        findings.append(Finding(
            id="transaction.connected_suite",
            category="trust",
            severity="high",
            status="unknown",
            title="Transaction attack suite is not verified",
            summary=f"0/{tx_total} transaction-integrity attacks were executed. Runtime availability alone is never scored as protection.",
            confidence=0.99,
            evidence=[_ev("transaction.connected_suite", {"tested": 0, "total": tx_total, "required_mode": "connected staging/runtime"}, source="external_scan", url=adapter_result.target_url)],
            recommendation=Recommendation(
                title="Run the connected transaction attack suite",
                why="Public discovery and UCP conformance do not prove replay, idempotency, signature, TOCTOU or payment-mutation enforcement.",
                how="Connect a staging/sandbox merchant runtime and execute the non-destructive transaction security tests.",
            ),
        ))

    if total == 0:
        findings.append(Finding(
            id="catalog.no_products",
            category="discovery",
            severity="medium",
            status="warning",
            title="No public product catalog was verified",
            summary="The scanner could not verify product records from structured data, a public commerce endpoint or bounded storefront cards.",
            confidence=0.80,
            evidence=[_ev("catalog.no_products", {"products": 0}, source="external_scan", url=adapter_result.target_url)],
            recommendation=Recommendation(
                title="Expose machine-readable products",
                why="Agent discovery is more reliable when product identity, image, price and availability are explicit.",
                how="Publish Product JSON-LD or connect the commerce platform for authoritative product evidence.",
            ),
        ))
    else:
        weak = [p for p in products if (p.ai_product_score or 0) < 80]
        if weak:
            missing_counts = Counter(field for p in weak for field in p.missing_fields)
            findings.append(Finding(
                id="catalog.product_gaps",
                category="product",
                severity="low",
                status="warning",
                title=f"{len(weak)} products have catalog evidence gaps",
                summary="Not verified in captured sources: " + (", ".join(x for x, _ in missing_counts.most_common(4)) or "product details") + ". Other store interfaces may contain these fields.",
                affected_count=len(weak),
                confidence=0.88,
                evidence=[_ev("catalog.product_gaps", {"affected": len(weak), "top_missing": missing_counts.most_common(10)}, source="external_scan", url=adapter_result.target_url)],
                recommendation=Recommendation(
                    title="Complete machine-readable product data",
                    why="Missing product fields can reduce agent matching accuracy, visual verification and checkout reliability.",
                    how="Review captured sources in the Catalog view before deciding whether merchant data is missing or the scanner needs another source.",
                ),
            ))

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    status_order = {"fail": 0, "warning": 1, "unknown": 2, "unsupported": 3, "pass": 4}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 9), status_order.get(f.status, 9), f.title.lower()))

    catalog_summary = build_catalog_summary(products, adapter_result.observations)

    return scores, findings, checks, products, {
        "products_scanned": total,
        "ai_ready_products": ready,
        "needs_improvement_products": needs,
        "high_risk_products": high_risk,
        "catalog_summary": catalog_summary,
    }
