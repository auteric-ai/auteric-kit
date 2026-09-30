from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field


SourceType = Literal["api", "external_scan", "merchant_input", "inferred", "runtime"]
FindingStatus = Literal["pass", "fail", "warning", "unsupported", "unknown"]
Severity = Literal["info", "low", "medium", "high", "critical"]
ScanStatus = Literal["queued", "running", "completed", "failed"]
CheckCategory = Literal["transport", "web", "ucp", "agent", "transaction", "catalog"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Capability(BaseModel):
    supported: bool
    source: SourceType
    confidence: float = Field(ge=0, le=1)
    status: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class NormalizedVariant(BaseModel):
    id: str
    title: str | None = None
    sku: str | None = None
    price: float | None = None
    currency: str | None = None
    available: bool | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class NormalizedProduct(BaseModel):
    id: str
    title: str
    description: str | None = None
    url: str | None = None
    price: float | None = None
    currency: str | None = None
    brand: str | None = None
    gtin: str | None = None
    sku: str | None = None
    availability: str | None = None
    images: list[str] = Field(default_factory=list)
    variants: list[NormalizedVariant] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    published: bool = True
    structured_data: bool = False
    evidence_scope: str = "catalog_record"
    evidence_urls: list[str] = Field(default_factory=list)
    source: SourceType = "external_scan"
    source_confidence: float = Field(default=0.6, ge=0, le=1)
    ai_product_score: int | None = None
    missing_fields: list[str] = Field(default_factory=list)
    passed_checks: list[str] = Field(default_factory=list)
    exposure: list[str] = Field(default_factory=list)
    field_checks: list[dict[str, Any]] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    type: str
    source: SourceType
    value: Any = None
    url: str | None = None
    product_id: str | None = None
    field: str | None = None
    timestamp: str = Field(default_factory=utc_now_iso)
    test_version: str = "security-scanner-3"


class Recommendation(BaseModel):
    title: str
    why: str
    how: str
    action_label: str = "Show me how to fix"


class Finding(BaseModel):
    id: str
    category: Literal["discovery", "product", "checkout", "channel", "security", "ucp", "transport", "trust"]
    severity: Severity
    status: FindingStatus
    title: str
    summary: str
    affected_count: int = 0
    confidence: float = Field(default=0.5, ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    recommendation: Recommendation | None = None


class SecurityCheck(BaseModel):
    id: str
    category: CheckCategory
    title: str
    method: str
    status: FindingStatus
    severity: Severity
    exposure: str
    result: str
    evidence: Any = None
    recommendation: str | None = None
    confidence: float = Field(default=0.9, ge=0, le=1)
    source: SourceType = "external_scan"


class ScoreBreakdown(BaseModel):
    discovery: int = 0
    product_quality: int = 0
    checkout: int = 0
    channel: int = 0
    ucp: int = 0
    protocol_conformance: int = 0
    web_security: int = 0
    agent_security: int | None = None
    transaction_security: int | None = None
    security: int = 0
    overall: int = 0
    revenue: int = 0
    coverage: int = 0
    transaction_coverage: int = 0


class AdapterResult(BaseModel):
    adapter: str
    platform: str
    store_name: str | None = None
    target_url: str
    products: list[NormalizedProduct] = Field(default_factory=list)
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)


class ScanResult(BaseModel):
    scan_id: str
    status: ScanStatus
    target_url: str
    adapter: str
    platform: str | None = None
    store_name: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    progress: int = 0
    progress_message: str = "Queued"
    scores: ScoreBreakdown | None = None
    products_scanned: int = 0
    ai_ready_products: int = 0
    needs_improvement_products: int = 0
    high_risk_products: int = 0
    findings: list[Finding] = Field(default_factory=list)
    checks: list[SecurityCheck] = Field(default_factory=list)
    products: list[NormalizedProduct] = Field(default_factory=list)
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    attack_surface: dict[str, Any] = Field(default_factory=dict)
    business_risks: dict[str, Any] = Field(default_factory=dict)
    catalog_summary: dict[str, Any] = Field(default_factory=dict)
    report_schema: str = "0.9"
    error: str | None = None


class ScanCreateRequest(BaseModel):
    target_url: str | None = None
    adapter: Literal["auto", "generic", "shopify", "woocommerce"] = "auto"
    platform_context: dict[str, Any] = Field(default_factory=dict)


class ScanCreateResponse(BaseModel):
    scan_id: str
    status: ScanStatus
