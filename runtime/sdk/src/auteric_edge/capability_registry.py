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
# Broader commerce inventory. These signatures are deliberately planned: discovery
# may recognize them now, but activation needs the matching engine release.
_EXTRA_PLANNED = (
 ("list_products", "catalog.products", "read", ("GET",), "/api/auteric/v2/products"),
 ("get_variant", "catalog.variants", "read", ("GET",), "/api/auteric/v2/variants/{variant_id}"),
 ("list_product_variants", "catalog.variants", "read", ("GET",), "/api/auteric/v2/products/{product_id}/variants"),
 ("get_inventory_availability", "inventory.availability", "read", ("GET",), "/api/auteric/v2/variants/{variant_id}/availability"),
 ("list_categories", "catalog.categories", "read", ("GET",), "/api/auteric/v2/categories"),
 ("list_brands", "catalog.brands", "read", ("GET",), "/api/auteric/v2/brands"),
 ("get_price", "pricing.current", "read", ("GET", "POST"), "/api/auteric/v2/prices/resolve"),
 ("get_price_list", "pricing.price-lists", "read", ("GET",), "/api/auteric/v2/price-lists/{price_list_id}"),
 ("quote_cart", "cart.quote", "read", ("POST",), "/api/auteric/v2/carts/{cart_id}/quote"),
 ("set_cart_note", "cart.notes", "write", ("PUT", "PATCH"), "/api/auteric/v2/carts/{cart_id}/note"),
 ("set_cart_currency", "cart.currency", "write", ("PUT", "PATCH"), "/api/auteric/v2/carts/{cart_id}/currency"),
 ("get_cart_shipping_address", "shipping.address", "read", ("GET",), "/api/auteric/v2/carts/{cart_id}/shipping-address"),
 ("set_cart_shipping_address", "shipping.address", "write", ("PUT", "PATCH"), "/api/auteric/v2/carts/{cart_id}/shipping-address"),
 ("get_cart_discount_codes", "discounts", "read", ("GET",), "/api/auteric/v2/carts/{cart_id}/discount-codes"),
 ("get_tax_quote", "tax.quote", "read", ("POST",), "/api/auteric/v2/carts/{cart_id}/tax-quote"),
 ("update_checkout", "checkout.details", "write", ("PUT", "PATCH"), "/api/auteric/v2/checkouts/{checkout_id}"),
 ("cancel_checkout", "checkout.cancel", "write", ("DELETE",), "/api/auteric/v2/checkouts/{checkout_id}"),
 ("lock_checkout", "checkout.lock", "write", ("POST",), "/api/auteric/v2/checkouts/{checkout_id}/lock"),
 ("list_payment_methods", "payments.methods", "read", ("GET",), "/api/auteric/v2/checkouts/{checkout_id}/payment-methods"),
 ("create_payment_intent", "payments.intent", "write", ("POST",), "/api/auteric/v2/checkouts/{checkout_id}/payment-intents"),
 ("get_payment_status", "payments.status", "read", ("GET",), "/api/auteric/v2/payments/{payment_id}"),
 ("get_customer", "customers.profile", "read", ("GET",), "/api/auteric/v2/customers/{customer_id}"),
 ("create_customer", "customers.profile", "write", ("POST",), "/api/auteric/v2/customers"),
 ("list_customer_addresses", "customers.addresses", "read", ("GET",), "/api/auteric/v2/customers/{customer_id}/addresses"),
 ("create_customer_address", "customers.addresses", "write", ("POST",), "/api/auteric/v2/customers/{customer_id}/addresses"),
 ("update_customer_address", "customers.addresses", "write", ("PUT", "PATCH"), "/api/auteric/v2/customers/{customer_id}/addresses/{address_id}"),
 ("delete_customer_address", "customers.addresses", "write", ("DELETE",), "/api/auteric/v2/customers/{customer_id}/addresses/{address_id}"),
 ("list_wishlist", "customers.wishlist", "read", ("GET",), "/api/auteric/v2/customers/{customer_id}/wishlist"),
 ("add_wishlist_item", "customers.wishlist", "write", ("POST",), "/api/auteric/v2/customers/{customer_id}/wishlist/items"),
 ("remove_wishlist_item", "customers.wishlist", "write", ("DELETE",), "/api/auteric/v2/customers/{customer_id}/wishlist/items/{product_id}"),
 ("cancel_order", "orders.cancel", "write", ("POST",), "/api/auteric/v2/orders/{order_id}/cancel"),
 ("get_order_history", "orders.history", "read", ("GET",), "/api/auteric/v2/orders/{order_id}/history"),
 ("get_fulfillment", "fulfillment", "read", ("GET",), "/api/auteric/v2/orders/{order_id}/fulfillments/{fulfillment_id}"),
 ("list_returns", "returns", "read", ("GET",), "/api/auteric/v2/returns"),
 ("cancel_return", "returns", "write", ("POST",), "/api/auteric/v2/returns/{return_id}/cancel"),
 ("list_subscriptions", "subscriptions", "read", ("GET",), "/api/auteric/v2/subscriptions"),
 ("get_subscription", "subscriptions", "read", ("GET",), "/api/auteric/v2/subscriptions/{subscription_id}"),
 ("pause_subscription", "subscriptions", "write", ("POST",), "/api/auteric/v2/subscriptions/{subscription_id}/pause"),
 ("resume_subscription", "subscriptions", "write", ("POST",), "/api/auteric/v2/subscriptions/{subscription_id}/resume"),
 ("cancel_subscription", "subscriptions", "write", ("POST",), "/api/auteric/v2/subscriptions/{subscription_id}/cancel"),
 ("get_gift_card_balance", "gift-cards", "read", ("GET",), "/api/auteric/v2/gift-cards/{gift_card_id}/balance"),
 ("apply_gift_card", "gift-cards", "write", ("POST",), "/api/auteric/v2/checkouts/{checkout_id}/gift-cards"),
 ("list_b2b_companies", "b2b.companies", "read", ("GET",), "/api/auteric/v2/b2b/companies"),
 ("get_b2b_company", "b2b.companies", "read", ("GET",), "/api/auteric/v2/b2b/companies/{company_id}"),
 ("list_b2b_locations", "b2b.locations", "read", ("GET",), "/api/auteric/v2/b2b/companies/{company_id}/locations"),
 ("create_quote", "b2b.quotes", "write", ("POST",), "/api/auteric/v2/b2b/quotes"),
 ("get_quote", "b2b.quotes", "read", ("GET",), "/api/auteric/v2/b2b/quotes/{quote_id}"),
)
_PATHS = {
 "search_products":"/api/auteric/v1/products/search", "get_product":"/api/auteric/v1/products/{product_id}", "create_cart":"/api/auteric/v1/carts", "get_cart":"/api/auteric/v1/carts/{cart_id}", "add_to_cart":"/api/auteric/v1/carts/{cart_id}/items", "update_cart_item":"/api/auteric/v1/carts/{cart_id}/items/{product_id}", "remove_from_cart":"/api/auteric/v1/carts/{cart_id}/items/{product_id}", "replace_cart_items":"/api/auteric/v1/carts/{cart_id}/items", "cancel_cart":"/api/auteric/v1/carts/{cart_id}", "create_checkout":"/api/auteric/v1/checkouts", "get_checkout":"/api/auteric/v1/checkouts/{checkout_id}",
 "list_collections":"/api/auteric/v1/collections", "get_collection":"/api/auteric/v1/collections/{collection_id}", "get_shipping_options":"/api/auteric/v1/carts/{cart_id}/shipping-options", "set_shipping_address":"/api/auteric/v1/checkouts/{checkout_id}/shipping-address", "select_shipping_option":"/api/auteric/v1/checkouts/{checkout_id}/shipping-option", "estimate_taxes":"/api/auteric/v1/checkouts/{checkout_id}/tax-estimate", "apply_discount_code":"/api/auteric/v1/carts/{cart_id}/discount-codes", "remove_discount_code":"/api/auteric/v1/carts/{cart_id}/discount-codes/{code}", "get_order":"/api/auteric/v1/orders/{order_id}", "list_orders":"/api/auteric/v1/orders", "get_fulfillment_tracking":"/api/auteric/v1/orders/{order_id}/tracking", "request_return":"/api/auteric/v1/orders/{order_id}/returns", "get_return":"/api/auteric/v1/returns/{return_id}", "get_loyalty_balance":"/api/auteric/v1/loyalty/balance", "redeem_loyalty_points":"/api/auteric/v1/loyalty/redemptions",
}
_BASE = ("reviewed_mapping", "contract_test", "policy", "runtime_activation")
CONTRACTS = {op: CapabilityContract(op, cap, side, verbs, _BASE + (("ownership", "idempotency") if side == "write" else ("ownership",)), desc) for op, cap, side, verbs, desc in _CURRENT}
PLANNED_CONTRACTS = {op: CapabilityContract(op, "auteric.commerce." + op, "write" if op.startswith(("set_", "select_", "apply_", "remove_", "request_", "redeem_")) else "read", ("POST",), ("engine_release",) + _BASE, "Reserved future contract.", "planned") for op in _PLANNED}
PLANNED_CONTRACTS.update({op: CapabilityContract(op, "auteric.commerce." + capability, side_effect, verbs, ("engine_release",) + _BASE + (("buyer_identity", "idempotency") if side_effect == "write" else ("buyer_identity",)), "Reserved future contract.", "planned") for op, capability, side_effect, verbs, _path in _EXTRA_PLANNED})
_PATHS.update({op: path for op, _capability, _side_effect, _verbs, path in _EXTRA_PLANNED})
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
