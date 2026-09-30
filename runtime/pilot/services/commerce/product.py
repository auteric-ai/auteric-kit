"""Merchant-facing product semantics projected from canonical commerce actions.

This module contains labels and lifecycle projection only. Runtime authority stays
in the canonical mapping, policy, gateway, job and connector layers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .adapter_manifest import contract_for


@dataclass(frozen=True)
class Capability:
    operation: str
    name: str
    description: str
    group: str
    side_effect: str
    default_enabled: bool = True


CAPABILITIES = (
    Capability("search_products", "Search products", "Find products in your catalog.", "Catalog", "read"),
    Capability("get_product", "Read product information", "Read current product, price and availability.", "Catalog", "read"),
    Capability("create_cart", "Create a cart", "Create an isolated shopping cart.", "Cart", "write"),
    Capability("get_cart", "Read a cart", "Read an agent-owned cart.", "Cart", "read"),
    Capability("add_to_cart", "Add products to a cart", "Add a product after policy checks.", "Cart", "write"),
    Capability("update_cart_item", "Change cart quantities", "Update quantity in an agent-owned cart.", "Cart", "write"),
    Capability("remove_from_cart", "Remove products from a cart", "Remove a line from an agent-owned cart.", "Cart", "write"),
    Capability("replace_cart_items", "Replace cart contents", "Replace all items in an agent-owned cart.", "Cart", "write", False),
    Capability("cancel_cart", "Cancel a cart", "Cancel an agent-owned cart.", "Cart", "write", False),
    Capability("create_checkout", "Start checkout", "Create a checkout handoff; does not complete payment.", "Checkout", "write"),
    Capability("get_checkout", "Read checkout status", "Read an agent-owned checkout.", "Checkout", "read"),
    Capability("update_checkout", "Update checkout", "Replace buyer, item and fulfillment checkout state.", "Checkout", "write", False),
    Capability("complete_checkout", "Complete checkout", "Complete an authorized checkout using a configured payment handler.", "Checkout", "write", False),
    Capability("cancel_checkout", "Cancel checkout", "Cancel an agent-owned checkout.", "Checkout", "write", False),
    Capability("get_order", "Read order", "Read an order created from an authorized checkout.", "Order", "read", False),
    Capability("apply_discount_code", "Apply discount code", "Apply a merchant-validated discount to a cart.", "Discount", "write", False),
    Capability("remove_discount_code", "Remove discount code", "Remove a discount code from a cart.", "Discount", "write", False),
    Capability("get_shipping_options", "Read shipping options", "Read authoritative fulfillment options for a cart.", "Fulfillment", "read", False),
    Capability("set_shipping_address", "Set shipping address", "Set the checkout fulfillment destination.", "Fulfillment", "write", False),
    Capability("select_shipping_option", "Select shipping option", "Select an authoritative checkout fulfillment option.", "Fulfillment", "write", False),
)
BY_OPERATION = {capability.operation: capability for capability in CAPABILITIES}
CORE_OPERATIONS = frozenset(
    {"search_products", "get_product", "create_cart", "get_cart", "add_to_cart", "create_checkout", "get_checkout"}
)


def mapping_state(rows: Iterable[dict], enabled: bool) -> str:
    """Project existing mapping evidence into one honest customer lifecycle state."""
    versions = list(rows)
    active = next((row for row in versions if row["state"] == "active"), None)
    if active:
        return "active" if enabled else "disabled"
    latest = versions[0] if versions else None
    if not latest:
        return "not_available"
    tests = latest.get("tests")
    if isinstance(tests, dict) and tests.get("status") == "pass":
        return "tested"
    if isinstance(tests, dict) and tests.get("status") == "fail":
        return "error"
    return "mapped"


def capability_view(
    rows: list[dict], enabled_operations: set[str], *, source: str = "generated_merchant_connector"
) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["operation"], []).append(row)
    result = []
    for capability in CAPABILITIES:
        versions = grouped.get(capability.operation, [])
        active = next((row for row in versions if row["state"] == "active"), None)
        latest = versions[0] if versions else None
        tests = (active or latest or {}).get("tests")
        tested = bool(isinstance(tests, dict) and tests.get("status") == "pass")
        result.append(
            {
                **asdict(capability),
                "source": source,
                "supported": bool(versions),
                "mapped": bool(versions),
                "tested": tested,
                "protected": bool(active),
                "state": mapping_state(versions, capability.operation in enabled_operations),
                "enabled": capability.operation in enabled_operations,
                "mapping_version": active["id"] if active else None,
                "adapter_contract": asdict(contract_for(capability.operation)),
            }
        )
    return result


def platform_setup(platform: str) -> dict:
    if platform == "custom":
        return {
            "kind": "skill",
            "available": True,
            "title": "Connect a custom store",
            "description": "Use a short-lived setup authorization to inspect and map existing commerce code.",
        }
    if platform == "shopify":
        return {
            "kind": "native_connector",
            "available": True,
            "title": "Shopify connector",
            "description": "Install Auteric with Shopify OAuth. The hosted Auteric connector handles catalog and cart access.",
        }
    if platform in {"wix", "woocommerce"}:
        return {
            "kind": "native_connector",
            "available": False,
            "title": f"{platform.title()} connector",
            "description": "Native OAuth/app installation is prepared as a connector type but is not shipped in this milestone.",
        }
    return {
        "kind": "custom_connector",
        "available": True,
        "title": "Connect your commerce backend",
        "description": "Use the connector SDK or setup assistant to map existing commerce functions.",
    }


def protocol_input_schema(model) -> dict:
    """Inline Pydantic definitions into the SDK's intentionally bounded schema subset."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if reference:
            prefix = "#/$defs/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise ValueError("Canonical schema contains an unsupported reference")
            name = reference.removeprefix(prefix)
            if name not in definitions:
                raise ValueError("Canonical schema reference is missing")
            merged = {**definitions[name], **{key: child for key, child in value.items() if key != "$ref"}}
            return inline(merged)
        return {key: inline(child) for key, child in value.items() if key != "$defs"}

    return inline(schema)
