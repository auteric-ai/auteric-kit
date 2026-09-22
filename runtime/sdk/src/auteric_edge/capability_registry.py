"""Reviewed, versioned Auteric capability contract pool."""
from dataclasses import asdict, dataclass

@dataclass(frozen=True)
class CapabilityContract:
    operation: str; capability: str; side_effect: str; http_methods: tuple[str, ...]
    activation_requirements: tuple[str, ...]; description: str; release_stage: str = "current"

_CURRENT = (
 ("search_products", "dev.ucp.shopping.catalog.search", "read", ("GET",), "Search the authoritative catalog."),
 ("get_product", "dev.ucp.shopping.catalog.lookup", "read", ("GET",), "Read one current product or variant."),
 ("create_cart", "dev.ucp.shopping.cart", "write", ("POST",), "Create an isolated agent-owned cart."),
 ("get_cart", "dev.ucp.shopping.cart", "read", ("GET",), "Read an agent-owned cart."),
 ("add_to_cart", "dev.ucp.shopping.cart", "write", ("POST",), "Add a sellable item to an agent-owned cart."),
 ("update_cart_item", "dev.ucp.shopping.cart", "write", ("PUT", "PATCH"), "Change an existing cart line quantity."),
 ("remove_from_cart", "dev.ucp.shopping.cart", "write", ("DELETE",), "Remove an existing cart line."),
 ("replace_cart_items", "dev.ucp.shopping.cart", "write", ("PUT",), "Atomically replace cart contents."),
 ("cancel_cart", "dev.ucp.shopping.cart", "write", ("DELETE", "POST"), "Cancel an agent-owned cart."),
 ("create_checkout", "dev.ucp.shopping.checkout", "write", ("POST",), "Create a human checkout handoff; never capture payment."),
 ("get_checkout", "dev.ucp.shopping.checkout", "read", ("GET",), "Read an agent-owned checkout handoff."),
)
_PLANNED = (
 "list_collections", "get_collection", "get_shipping_options", "set_shipping_address", "select_shipping_option",
 "estimate_taxes", "apply_discount_code", "remove_discount_code", "get_order", "list_orders", "get_fulfillment_tracking",
 "request_return", "get_return", "get_loyalty_balance", "redeem_loyalty_points",
)
_PATHS = {
 "search_products":"/api/auteric/v1/products/search", "get_product":"/api/auteric/v1/products/{product_id}", "create_cart":"/api/auteric/v1/carts", "get_cart":"/api/auteric/v1/carts/{cart_id}", "add_to_cart":"/api/auteric/v1/carts/{cart_id}/items", "update_cart_item":"/api/auteric/v1/carts/{cart_id}/items/{product_id}", "remove_from_cart":"/api/auteric/v1/carts/{cart_id}/items/{product_id}", "replace_cart_items":"/api/auteric/v1/carts/{cart_id}/items", "cancel_cart":"/api/auteric/v1/carts/{cart_id}", "create_checkout":"/api/auteric/v1/checkouts", "get_checkout":"/api/auteric/v1/checkouts/{checkout_id}",
 "list_collections":"/api/auteric/v1/collections", "get_collection":"/api/auteric/v1/collections/{collection_id}", "get_shipping_options":"/api/auteric/v1/carts/{cart_id}/shipping-options", "set_shipping_address":"/api/auteric/v1/checkouts/{checkout_id}/shipping-address", "select_shipping_option":"/api/auteric/v1/checkouts/{checkout_id}/shipping-option", "estimate_taxes":"/api/auteric/v1/checkouts/{checkout_id}/tax-estimate", "apply_discount_code":"/api/auteric/v1/carts/{cart_id}/discount-codes", "remove_discount_code":"/api/auteric/v1/carts/{cart_id}/discount-codes/{code}", "get_order":"/api/auteric/v1/orders/{order_id}", "list_orders":"/api/auteric/v1/orders", "get_fulfillment_tracking":"/api/auteric/v1/orders/{order_id}/tracking", "request_return":"/api/auteric/v1/orders/{order_id}/returns", "get_return":"/api/auteric/v1/returns/{return_id}", "get_loyalty_balance":"/api/auteric/v1/loyalty/balance", "redeem_loyalty_points":"/api/auteric/v1/loyalty/redemptions",
}
_BASE = ("reviewed_mapping", "contract_test", "policy", "runtime_activation")
CONTRACTS = {op: CapabilityContract(op, cap, side, verbs, _BASE + (("ownership", "idempotency") if side == "write" else ("ownership",)), desc) for op, cap, side, verbs, desc in _CURRENT}
PLANNED_CONTRACTS = {op: CapabilityContract(op, "auteric.commerce." + op, "write" if op.startswith(("set_", "select_", "apply_", "remove_", "request_", "redeem_")) else "read", ("POST",), ("engine_release",) + _BASE, "Reserved future contract.", "planned") for op in _PLANNED}
CANONICAL_OPERATIONS = tuple(CONTRACTS)

def contract(operation):
    if operation not in CONTRACTS: raise ValueError("Unsupported canonical operation")
    return CONTRACTS[operation]
def any_contract(operation):
    return CONTRACTS.get(operation) or PLANNED_CONTRACTS.get(operation) or (_ for _ in ()).throw(ValueError("Unknown capability"))
def canonical_path(operation): any_contract(operation); return _PATHS[operation]
def registry_document():
    entries = []
    for item in (*CONTRACTS.values(), *PLANNED_CONTRACTS.values()):
        entries.append({**asdict(item), "tool_name": item.operation, "path": canonical_path(item.operation), "contract_version":"v1", "http_methods":list(item.http_methods), "activation_requirements":list(item.activation_requirements)})
    return {"schema_version":"auteric.capability-contract-pool.v2", "contracts":entries, "current_runtime_operations":list(CONTRACTS), "planned_operations":list(PLANNED_CONTRACTS), "signature_issuer":"Auteric control plane only", "selection_rule":"Select only current contracts with reviewed binding, tests, policy and runtime activation.", "code_change_reconciliation":{"event":"auteric.merchant.source.updated.v1", "action":"Retest changed bindings before exposure changes."}}
