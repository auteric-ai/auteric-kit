"""Small deterministic ruleset. Missing evidence is never interpreted as zero risk."""
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from .actions import CommerceAction, ActionType, Model


class Outcome(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class Decision(Model):
    outcome: Outcome
    reasons: list[str]
    matched_rules: list[str]
    context: dict


class PolicyConfig(Model):
    allow_percent: Decimal = Field(default=Decimal("10"), ge=0, le=100, allow_inf_nan=False)
    approval_percent: Decimal = Field(default=Decimal("30"), gt=0, le=100, allow_inf_nan=False)
    bulk_approval_count: int = Field(default=50, ge=1, strict=True)
    write_roles: list[str] = Field(default_factory=lambda: ["merchandising_manager", "admin"], min_length=1)
    read_roles: list[str] = Field(default_factory=lambda: ["analyst", "merchandising_manager", "admin"], min_length=1)
    unknown_read_allowed: bool = True
    minimum_price: Decimal = Field(default=Decimal("0.01"), gt=0, allow_inf_nan=False)
    approval_ttl_seconds: int = Field(default=900, ge=30, le=86400, strict=True)
    supported_currencies: list[str] = Field(default_factory=lambda: ["USD", "EUR", "GBP", "ILS", "CAD", "AUD"], min_length=1)

    @model_validator(mode="after")
    def ordered_thresholds(self):
        if self.allow_percent >= self.approval_percent:
            raise ValueError("allow_percent must be below approval_percent")
        if any(not role.strip() for role in self.write_roles + self.read_roles):
            raise ValueError("Policy roles must not be blank")
        return self


def build_context(action: CommerceAction) -> dict:
    rows = []
    totals: dict[str, dict] = {}
    for item in action.items:
        old, new, quantity = item.before, item.proposed_after, item.inventory_quantity
        delta = new - old if old is not None and new is not None else None
        percent = delta * 100 / old if delta is not None and old is not None and old > 0 else None
        exposure = abs(delta) * quantity if delta is not None and quantity is not None else None
        old_value = old * quantity if old is not None and quantity is not None else None
        rows.append({
            "resource_id": item.resource_id, "product_id": item.product_id, "title": item.title,
            "sku": item.sku, "currency": item.currency,
            "old_price": str(old) if old is not None else None,
            "new_price": str(new) if new is not None else None,
            "absolute_change": str(abs(delta)) if delta is not None else None,
            "percentage_change": str(percent) if percent is not None else None,
            "direction": None if delta is None else "decrease" if delta < 0 else "increase" if delta > 0 else "unchanged",
            "inventory_quantity": quantity,
            "estimated_inventory_value": str(old_value) if old_value is not None else None,
            "estimated_revenue_exposure": str(exposure) if exposure is not None else None,
        })
        currency = item.currency or "UNKNOWN"
        total = totals.setdefault(currency, {"estimated_inventory_value": Decimal(0), "estimated_revenue_exposure": Decimal(0), "inventory_quantity": 0, "complete": True})
        if old_value is None or exposure is None or quantity is None:
            total["complete"] = False
        else:
            total["estimated_inventory_value"] += old_value
            total["estimated_revenue_exposure"] += exposure
            total["inventory_quantity"] += quantity
    for total in totals.values():
        for field in ("estimated_inventory_value", "estimated_revenue_exposure", "inventory_quantity"):
            total[field] = (str(total[field]) if field != "inventory_quantity" else total[field]) if total["complete"] else None
    return {"items": rows, "affected_resource_count": len(action.items),
            "affected_product_count": len({i.product_id or i.resource_id for i in action.items}),
            "affected_skus": sorted({i.sku for i in action.items if i.sku}),
            "totals_by_currency": totals,
            "estimate_notice": "Inventory-value sensitivity estimate, not a revenue or realized-loss prediction.",
            "principal_known": bool(action.principal.subject and action.principal.subject.strip()
                                    and action.principal.subject.strip().lower() not in {"unknown", "anonymous"})}


def evaluate(action: CommerceAction, policy: PolicyConfig | None = None) -> Decision:
    policy = policy or PolicyConfig()
    context = build_context(action)
    findings: list[tuple[Outcome, str, str]] = []

    def add(outcome, rule, reason):
        if rule not in [f[1] for f in findings]:
            findings.append((outcome, rule, reason))

    read = action.type.value.endswith(".read")
    known = context["principal_known"]
    if not known and not (read and policy.unknown_read_allowed):
        add(Outcome.BLOCK, "unknown-principal", "Sensitive actions require an identified human principal.")
    elif known and not set(action.principal.roles).intersection(policy.read_roles if read else policy.write_roles):
        add(Outcome.BLOCK, "prohibited-role", "Principal role does not authorize this commerce action.")

    scope = "pricing" if action.type in {ActionType.PRICE_READ, ActionType.PRICE_UPDATE} else action.type.value.split(".")[0]
    required_scope = f"{scope}.{'read' if read else 'write'}"
    if action.delegation is not None:
        if required_scope not in action.delegation.scopes:
            add(Outcome.BLOCK, "delegation-scope", f"Delegation does not grant {required_scope}.")
        if action.delegation.expires_at is not None and action.delegation.expires_at <= datetime.now(timezone.utc):
            add(Outcome.BLOCK, "delegation-expired", "Delegated authority has expired.")

    if read:
        add(Outcome.ALLOW, "read-observability", "Read action recorded; upstream backend must enforce resource-level access.")
    elif action.type != ActionType.PRICE_UPDATE:
        add(Outcome.BLOCK, "unsupported-write", "This sensitive action type has no enabled execution policy in this milestone.")
    else:
        if not action.items:
            add(Outcome.BLOCK, "empty-price-action", "Price update must identify at least one resource.")
        if len(action.items) > policy.bulk_approval_count:
            add(Outcome.REQUIRE_APPROVAL, "bulk-price-change", f"More than {policy.bulk_approval_count} affected resources require approval.")
        for item in action.items:
            if not item.sku or not item.sku.strip():
                add(Outcome.BLOCK, "missing-sku", "SKU is required for each executable price change.")
            if item.currency not in policy.supported_currencies:
                add(Outcome.BLOCK, "unsupported-currency", "A supported two-decimal currency is required for each item.")
            if item.inventory_quantity is None:
                add(Outcome.BLOCK, "unknown-inventory", "Inventory quantity is unknown; economic exposure cannot be evaluated reliably.")
            if item.before is None or item.before <= 0 or item.proposed_after is None:
                add(Outcome.BLOCK, "invalid-price-context", "Positive current price and a proposed price are required.")
                continue
            if item.proposed_after < policy.minimum_price:
                add(Outcome.BLOCK, "minimum-price", f"Proposed price is below merchant minimum {policy.minimum_price}.")
            percent = abs(item.proposed_after - item.before) * 100 / item.before
            if percent > policy.approval_percent:
                add(Outcome.BLOCK, "extreme-price-change", f"Price movement {percent}% exceeds {policy.approval_percent}% merchant limit.")
            elif percent > policy.allow_percent:
                add(Outcome.REQUIRE_APPROVAL, "large-price-change", f"Price movement {percent}% exceeds {policy.allow_percent}% automatic-allow limit.")
        if not findings:
            add(Outcome.ALLOW, "small-price-change", f"Price movement is within {policy.allow_percent}% and principal authority is valid.")
    outcome = max((f[0] for f in findings), key=lambda value: {Outcome.ALLOW: 0, Outcome.REQUIRE_APPROVAL: 1, Outcome.BLOCK: 2}[value])
    return Decision(outcome=outcome, reasons=[f[2] for f in findings], matched_rules=[f[1] for f in findings], context=context)
