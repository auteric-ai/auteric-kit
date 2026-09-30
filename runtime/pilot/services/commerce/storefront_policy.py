"""Deterministic storefront controls, independent of agent provider and transport.

Context must be assembled from authenticated identity and merchant reads by the host.
Client-provided price, category, identity and cart totals are never authority.
"""

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StorefrontPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1)
    max_quantity_per_item: int = Field(default=10, ge=1, le=10000, strict=True)
    max_cart_quantity: int = Field(default=50, ge=1, le=100000, strict=True)
    max_cart_value: Decimal = Field(default=Decimal("5000"), gt=0, allow_inf_nan=False)
    approval_cart_value: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    restricted_skus: list[str] = Field(default_factory=list, max_length=10000)
    restricted_categories: list[str] = Field(default_factory=list, max_length=10000)
    require_verified_agent: bool = True
    max_requests_per_minute: int = Field(default=120, ge=1, le=10000, strict=True)
    allowed_agent_ids: list[str] = Field(default_factory=list, max_length=1000)
    # FIXED denial shape when a principal touches a resource owned by another
    # principal: 404 (default; foreign resources are indistinguishable from
    # nonexistent) or 403. Never per-request (plan §11).
    ownership_denial: Literal["not_found", "forbidden"] = "not_found"

    @model_validator(mode="after")
    def thresholds(self):
        if any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for value in self.allowed_agent_ids):
            raise ValueError('Allowed agents must be exact issued credential IDs, not names or headers')
        if self.approval_cart_value is not None and self.approval_cart_value >= self.max_cart_value:
            raise ValueError("Approval threshold must be below the blocking cart value")
        return self


READS = frozenset({"search_products", "get_product", "lookup_products", "get_cart", "get_checkout", "get_order", "get_shipping_options"})
WRITES = frozenset(
    {
        "create_cart",
        "add_to_cart",
        "update_cart_item",
        "remove_from_cart",
        "replace_cart_items",
        "cancel_cart",
        "create_checkout",
        "update_checkout",
        "complete_checkout",
        "cancel_checkout",
        "apply_discount_code",
        "remove_discount_code",
        "set_shipping_address",
        "select_shipping_option",
    }
)


def evaluate(
    operation: str, request: dict, policy: StorefrontPolicy | dict | None = None, context: dict | None = None
) -> dict:
    """Return an explainable decision. Exceptions/missing authority never allow writes.

    Host context: agent_verified:bool, session_id:str, request_count_last_minute:int,
    products:{id:Product}, cart:Cart for existing-cart mutations/checkouts. Category
    rules use Product.metadata.categories. A cart read is authorized by host ownership.
    This function does not trust arbitrary projected-total input from a caller.
    """
    cfg = policy if isinstance(policy, StorefrontPolicy) else StorefrontPolicy.model_validate(policy or {})
    ctx = context or {}
    findings: list[tuple[str, str, str]] = []

    def issue(rule, reason, outcome="BLOCK"):
        if rule not in [f[0] for f in findings]:
            findings.append((rule, reason, outcome))

    if operation not in READS | WRITES:
        issue("unsupported-operation", "This operation is not enabled.")
    if cfg.require_verified_agent and ctx.get("agent_verified") is not True:
        issue("unknown-agent", "A verified agent credential is required; a profile URL is not proof of identity.")
    if cfg.allowed_agent_ids and ctx.get('operator_test') is not True and ctx.get('agent_credential_id') not in cfg.allowed_agent_ids:
        issue('agent-not-allowed', 'The authenticated credential is not in the Store agent allowlist.')
    if not isinstance(ctx.get("session_id"), str) or not ctx["session_id"].strip():
        issue("missing-session", "An authenticated session binding is required.")
    count = ctx.get("request_count_last_minute")
    if count is not None and (type(count) is not int or count < 0):
        issue("invalid-rate-context", "Rate-limit context is invalid.")
    elif count is not None and count > cfg.max_requests_per_minute:
        issue("rate-limit", "Agent request rate exceeds merchant policy.")

    computed: dict[str, Any] = {"policy_version": cfg.version}
    if operation in WRITES:
        try:
            cart = ctx.get("cart")
            if operation not in {"create_cart"}:
                expected_cart = request.get("cart_id") or ctx.get("checkout", {}).get("cart_id")
                if not isinstance(cart, dict) or cart.get("id") != expected_cart:
                    issue("missing-cart-context", "Current merchant cart must be read and bound before this operation.")
                    cart = None
                elif cart.get("status", cart.get("metadata", {}).get("status")) in {
                    "canceled",
                    "completed",
                    "checked_out",
                }:
                    issue("invalid-cart-state", "Cart state does not permit this operation.")
            current_items = list(cart.get("items", [])) if cart else []
            proposed = [
                {"product_id": i["product_id"], "variant_id": i.get("variant_id"), "quantity": i["quantity"]}
                for i in current_items
            ]
            if operation in {"create_cart", "replace_cart_items", "update_checkout"}:
                proposed = request.get("items", [])
            elif operation in {"add_to_cart", "update_cart_item"}:
                key = request["product_id"]
                variant = request.get("variant_id")
                match = next(
                    (
                        i
                        for i in proposed
                        if i["product_id"] == key and (variant is None or i.get("variant_id") == variant)
                    ),
                    None,
                )
                quantity = request["quantity"]
                if match:
                    match["quantity"] = quantity + match["quantity"] if operation == "add_to_cart" else quantity
                elif operation == "add_to_cart":
                    proposed.append({"product_id": key, "variant_id": variant, "quantity": quantity})
                else:
                    issue("missing-cart-item", "Cannot update an item that is absent from the cart.")
            elif operation == "remove_from_cart":
                proposed = [i for i in proposed if i["product_id"] != request["product_id"]]
            elif operation in {"cancel_cart", "cancel_checkout"}:
                proposed = []
            if not isinstance(proposed, list):
                raise ValueError("Items must be a list")
            products = ctx.get("products", {})
            total, quantity_total = Decimal(0), 0
            seen = set()
            for item in proposed:
                key = item.get("variant_id") or item["product_id"]
                if key in seen:
                    issue("duplicate-item", "Duplicate resource lines must be normalized before authorization.")
                seen.add(key)
                quantity = item["quantity"]
                if type(quantity) is not int or quantity < 1:
                    issue("invalid-quantity", "Quantity must be a positive integer.")
                    continue
                quantity_total += quantity
                if quantity > cfg.max_quantity_per_item:
                    issue("item-quantity-limit", f"An item exceeds the {cfg.max_quantity_per_item}-unit limit.")
                product = products.get(key)
                if not isinstance(product, dict) or product.get("id") != key:
                    issue("missing-product-context", "Authoritative product data is missing.")
                    continue
                if product.get("sku") in cfg.restricted_skus:
                    issue("restricted-sku", "A requested SKU is restricted by merchant policy.")
                categories = product.get("metadata", {}).get("categories")
                if cfg.restricted_categories:
                    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
                        issue("unknown-category", "Category controls require authoritative category data.")
                    elif set(categories) & set(cfg.restricted_categories):
                        issue("restricted-category", "A requested category is restricted by merchant policy.")
                if product.get("currency") != cfg.currency:
                    issue("currency-mismatch", "Product currency does not match the policy currency.")
                price = Decimal(str(product["price"]))
                if not price.is_finite() or price < 0:
                    raise ValueError("Invalid price")
                total += price * quantity
                if product.get("availability") != "in_stock":
                    issue("product-unavailable", "Product availability does not permit purchase.")
                inventory = product.get("inventory")
                if inventory is not None and (type(inventory) is not int or inventory < quantity):
                    issue("insufficient-inventory", "Requested quantity exceeds available inventory.")
            if quantity_total > cfg.max_cart_quantity:
                issue("cart-quantity-limit", f"Cart exceeds the {cfg.max_cart_quantity}-unit limit.")
            # Merchant totals include tax/shipping; use the larger value for checkout.
            if operation in {"create_checkout", "update_checkout", "complete_checkout"} and cart:
                if cart.get("currency") != cfg.currency:
                    issue("currency-mismatch", "Cart currency does not match the policy currency.")
                cart_total = Decimal(str(cart["total"]))
                if not cart_total.is_finite() or cart_total < 0:
                    raise ValueError("Invalid cart total")
                total = max(total, cart_total)
                if not proposed:
                    issue("empty-checkout", "Checkout cannot be created from an empty cart.")
            if operation in {"update_checkout", "complete_checkout", "cancel_checkout", "set_shipping_address", "select_shipping_option"}:
                checkout = ctx.get("checkout")
                if not isinstance(checkout, dict) or checkout.get("id") != request.get("checkout_id"):
                    issue("missing-checkout-context", "Current merchant checkout must be read and bound before this operation.")
                elif checkout.get("status") in {"completed", "canceled"}:
                    issue("invalid-checkout-state", "Checkout state does not permit this operation.")
                elif operation == "complete_checkout" and checkout.get("status") not in {"ready_for_complete", "requires_escalation"}:
                    issue("checkout-not-ready", "Checkout is not ready for completion.")
            if total > cfg.max_cart_value:
                issue("cart-value-limit", f"Cart value exceeds the {cfg.max_cart_value} {cfg.currency} limit.")
            elif cfg.approval_cart_value is not None and total > cfg.approval_cart_value:
                issue("cart-value-approval", "Cart value requires approval of this exact action.", "REQUIRE_APPROVAL")
            computed.update(projected_subtotal=str(total), currency=cfg.currency, quantity=quantity_total)
        except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError):
            issue("invalid-business-context", "Merchant context is incomplete or invalid; no write is authorized.")
    outcome = "BLOCK" if any(f[2] == "BLOCK" for f in findings) else "REQUIRE_APPROVAL" if findings else "ALLOW"
    return {
        "outcome": outcome,
        "matched_rules": [f[0] for f in findings] or ["storefront-allow"],
        "reasons": [f[1] for f in findings] or ["Authenticated action is within the configured storefront policy."],
        "context": computed,
    }
