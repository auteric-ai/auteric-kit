"""Versioned, fail-closed adapter requirements for canonical commerce actions.

This is deliberately an Auteric control-plane contract, not a claim that a
merchant platform implements every UCP capability.  A manifest describes what
must be true before a tested mapping may become active; the contract-test
receipt supplies the per-mapping evidence.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

SCHEMA_VERSION = "auteric.adapter-manifest.v1"


@dataclass(frozen=True)
class AdapterContract:
    operation: str
    side_effect: str
    requires_verified_agent: bool
    requires_idempotency: bool
    authoritative_recheck: bool
    requires_cart_ownership: bool
    variant_semantics: str
    fulfillment_semantics: str
    checkout_boundary: str


_READ = {"search_products", "get_product", "get_cart", "get_checkout", "get_order", "get_shipping_options"}
_CART_MUTATIONS = {"add_to_cart", "update_cart_item", "remove_from_cart", "replace_cart_items", "cancel_cart", "apply_discount_code", "remove_discount_code"}
_CART = {"create_cart", "get_cart", *_CART_MUTATIONS}
_CHECKOUT = {"create_checkout", "get_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}
_FULFILLMENT = {"get_shipping_options", "set_shipping_address", "select_shipping_option"}


def contract_for(operation: str) -> AdapterContract:
    if operation not in _READ | _CART | _CHECKOUT | _FULFILLMENT:
        raise ValueError("Unsupported canonical commerce operation")
    write = operation not in _READ
    return AdapterContract(
        operation=operation,
        side_effect="write" if write else "read",
        requires_verified_agent=write,
        requires_idempotency=write,
        authoritative_recheck=write,
        requires_cart_ownership=operation in _CART_MUTATIONS | {"get_cart", "get_shipping_options"} | _CHECKOUT | _FULFILLMENT,
        variant_semantics=(
            "authoritative_when_supplied"
            if operation in {
                "search_products", "get_product", "create_cart", "add_to_cart",
                "update_cart_item", "replace_cart_items",
            }
            else "not_applicable"
        ),
        fulfillment_semantics="merchant_authoritative" if operation in _CHECKOUT | _FULFILLMENT else "not_applicable",
        checkout_boundary=(
            "configured_payment_handler_required" if operation == "complete_checkout"
            # Activation may only ever claim the merchant-handoff pattern for
            # checkout: a merchant payment URL handoff is supported, but the
            # platform never claims autonomous payment completion for these
            # mappings (plan §13.4).
            else "merchant_handoff_only" if operation in _CHECKOUT else "not_applicable"
        ),
    )


def manifest_for(operation: str, mapping_fingerprint: str) -> dict:
    """Build a stable, non-secret manifest bound to one exact mapping version."""
    contract = asdict(contract_for(operation))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mapping_fingerprint": mapping_fingerprint,
        "contract": contract,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**payload, "digest": digest}


def activation_requirements(operation: str) -> list[str]:
    contract = contract_for(operation)
    requirements = ["canonical_response_schema", "connector_connectivity", "merchant_request"]
    if contract.requires_idempotency:
        requirements.append("idempotency_replay")
    if contract.authoritative_recheck:
        requirements.append("authoritative_context_recheck")
    if contract.requires_cart_ownership:
        requirements.append("merchant_resource_ownership")
    if contract.variant_semantics != "not_applicable":
        requirements.append("variant_identity_and_availability")
    if contract.fulfillment_semantics != "not_applicable":
        requirements.append("merchant_fulfillment_totals")
    return requirements
