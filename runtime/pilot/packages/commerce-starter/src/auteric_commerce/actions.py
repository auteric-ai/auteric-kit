"""Provider-independent commerce intent. Identity is supplied by trusted hosts, not models."""
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionType(StrEnum):
    CATALOG_SEARCH = "catalog.search"
    CART_CREATE = "cart.create"
    CART_ADD_ITEM = "cart.add_item"
    CART_UPDATE_ITEM = "cart.update_item"
    CART_REMOVE_ITEM = "cart.remove_item"
    CART_CANCEL = "cart.cancel"
    PRODUCT_READ = "product.read"
    PRODUCT_UPDATE = "product.update"
    PRICE_READ = "price.read"
    PRICE_UPDATE = "price.update"
    INVENTORY_READ = "inventory.read"
    INVENTORY_UPDATE = "inventory.update"
    DISCOUNT_CREATE = "discount.create"
    DISCOUNT_UPDATE = "discount.update"
    DISCOUNT_DELETE = "discount.delete"
    DISCOUNT_APPLY = "discount.apply"
    DISCOUNT_REMOVE = "discount.remove"
    PROMOTION_CREATE = "promotion.create"
    PROMOTION_UPDATE = "promotion.update"
    PROMOTION_DELETE = "promotion.delete"
    ORDER_READ = "order.read"
    ORDER_UPDATE = "order.update"
    ORDER_CANCEL = "order.cancel"
    REFUND_CREATE = "refund.create"
    CUSTOMER_READ = "customer.read"
    CART_READ = "cart.read"
    CART_UPDATE = "cart.update"
    CHECKOUT_CREATE = "checkout.create"
    CHECKOUT_READ = "checkout.read"
    CHECKOUT_UPDATE = "checkout.update"
    CHECKOUT_COMPLETE = "checkout.complete"
    CHECKOUT_CANCEL = "checkout.cancel"
    FULFILLMENT_OPTIONS_READ = "fulfillment.options.read"
    FULFILLMENT_ADDRESS_SET = "fulfillment.address.set"
    FULFILLMENT_OPTION_SELECT = "fulfillment.option.select"


class ActionPrincipal(Model):
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    roles: list[str] = Field(default_factory=list, max_length=30)


class AgentIdentity(Model):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    provider: str | None = Field(default=None, max_length=100)


class Delegation(Model):
    scopes: list[str] = Field(default_factory=list, max_length=100)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Delegation expiration must include timezone")
        return value


Money = Annotated[Decimal, Field(allow_inf_nan=False, max_digits=18, decimal_places=2)]


class PriceItem(Model):
    resource_id: str = Field(min_length=1, max_length=200)
    product_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=500)
    before: Money | None = None
    proposed_after: Money | None = None
    sku: str | None = Field(default=None, max_length=200)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    inventory_quantity: int | None = Field(default=None, strict=True, ge=0)


class CommerceAction(Model):
    action_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    trace_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    merchant_id: str = Field(min_length=1, max_length=200)
    source: str = Field(default="custom", min_length=1, max_length=100)
    protocol: str = Field(default="python", min_length=1, max_length=100)
    principal: ActionPrincipal = Field(default_factory=ActionPrincipal)
    agent: AgentIdentity = Field(default_factory=AgentIdentity)
    app_id: str | None = Field(default=None, max_length=200)
    delegation: Delegation | None = None
    type: ActionType
    items: list[PriceItem] = Field(default_factory=list, max_length=10000)
    intent: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    merchant_context: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def timestamp_timezone(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Action timestamp must include timezone")
        return value

    @model_validator(mode="after")
    def unique_resources(self):
        ids = [item.resource_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate resource IDs are not valid in one action")
        return self


def action_fingerprint(action: CommerceAction) -> str:
    """Bind approval to the full normalized action, including identities and tenant."""
    encoded = json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()
