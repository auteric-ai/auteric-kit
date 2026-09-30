from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Principal(StrictModel):
    subject: str = Field(min_length=1)
    role: Literal["agent", "approver", "executor"]
    token: SecretStr
    # Business authority is configured by the host, never accepted from the model.
    human_subject: str | None = None
    business_roles: list[str] = Field(default_factory=list)
    agent_id: str | None = None
    delegation_scopes: list[str] | None = None
    delegation_expires_at: str | None = None


class Settings(StrictModel):
    merchant_id: str = Field(min_length=1)
    database: str = ".runtime/gateway.db"
    mode: Literal["demo", "shopify"] = "demo"
    principals: list[Principal] = Field(min_length=3)
    max_delta_percent: Decimal = Field(default=Decimal("15"), gt=0, le=50)
    minimum_price: Decimal = Field(default=Decimal("1"), gt=0)
    proposal_ttl_seconds: int = Field(default=900, ge=30, le=3600)
    live_writes: bool = False
    shop_domain: str | None = None
    shopify_token: SecretStr | None = None
    api_version: Literal["2026-07"] = "2026-07"
    legacy_gateway_enabled: bool = False
    local_dev: bool = False
    runtime_policy_path: str | None = None

    @model_validator(mode="after")
    def validate_credentials(self):
        if self.local_dev and self.mode != "demo":
            raise ValueError("Local developer console is restricted to the synthetic demo")
        if self.local_dev and self.legacy_gateway_enabled:
            raise ValueError("Developer runtime cannot enable legacy mutation routes")
        tokens = [p.token.get_secret_value() for p in self.principals]
        if len(set(tokens)) != len(tokens) or any(len(t) < 32 or t.startswith('REPLACE_') for t in tokens):
            raise ValueError("Use distinct random bearer tokens of at least 32 characters")
        if len({p.subject for p in self.principals}) != len(self.principals):
            raise ValueError("Each credential requires a distinct subject")
        if {p.role for p in self.principals} != {"agent", "approver", "executor"}:
            raise ValueError("Configure agent, approver and executor principals")
        if self.mode == "shopify" and (not self.shop_domain or not self.shopify_token):
            raise ValueError("Shopify domain and server-side token required")
        if self.shopify_token and self.shopify_token.get_secret_value().startswith('REPLACE_'):
            raise ValueError("Replace the example Shopify credential before startup")
        return self


class PriceProposal(StrictModel):
    variant_id: str = Field(min_length=1, max_length=160)
    new_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class Approval(StrictModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Variant(StrictModel):
    id: str
    product_id: str
    title: str
    sku: str
    price: Decimal
    currency: str
    stock: int


class DomainError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def enforce_policy(settings: Settings, variant: Variant, price: Decimal):
    if not variant.sku.strip():
        raise DomainError("SKU is required for an executable price change", 422)
    if variant.currency not in {"USD", "EUR", "GBP", "ILS", "CAD", "AUD"}:
        raise DomainError("Currency not supported by this two-decimal price adapter", 422)
    if not variant.price.is_finite() or variant.price <= 0:
        raise DomainError("Current price is invalid", 422)
    if price < settings.minimum_price:
        raise DomainError("Proposed price is below merchant minimum", 422)
    if abs(price - variant.price) * 100 / variant.price > settings.max_delta_percent:
        raise DomainError("Proposed price exceeds merchant movement limit", 422)
