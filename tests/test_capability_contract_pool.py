import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime/sdk/src"))
from auteric_edge.capability_registry import canonical_path, registry_document
from auteric_edge.onboarding import inventory

def test_registry_contains_current_and_planned_contracts_with_stable_paths():
    document = registry_document()
    assert document["schema_version"] == "auteric.capability-contract-pool.v2"
    assert len(document["contracts"]) == 74
    assert set(document["current_runtime_operations"]) == {
        "search_products", "get_product", "create_cart", "get_cart", "add_to_cart",
        "update_cart_item", "remove_from_cart", "replace_cart_items", "cancel_cart",
        "create_checkout", "get_checkout", "update_checkout", "complete_checkout",
        "cancel_checkout", "get_order",
        "apply_discount_code", "remove_discount_code", "get_shipping_options",
        "set_shipping_address", "select_shipping_option",
    }
    assert canonical_path("create_cart") == "/api/auteric/v1/carts"
    assert all(item["path"].startswith("/api/auteric/v") for item in document["contracts"])

def test_inventory_emits_installation_binding_plan(tmp_path):
    (tmp_path / "app.js").write_text("app.post('/api/carts', handler);")
    report = inventory(tmp_path)
    assert report["installation_binding_plan"] == [{
        "operation": "create_cart", "canonical_path": "create_cart", "contract_version": "v1",
        "state": "requires_adapter_review", "merchant_candidates": [{
            "method": "POST", "route": "/api/carts", "source": "app.js", "line": 1,
            "confidence": "requires_review",
        }],
    }]
    assert report["merchant_capability_inventory"][0]["mcp_exposure"] == "eligible_after_adapter_validation"

def test_inventory_retains_planned_and_merchant_capabilities_without_exposure(tmp_path):
    (tmp_path / "app.js").write_text("app.get('/api/shipping-methods', handler);\napp.post('/api/payments', handler);")
    report = inventory(tmp_path)
    capabilities = {item["capability_id"]: item for item in report["merchant_capability_inventory"]}
    assert capabilities["get_shipping_options"]["classification"] == "runtime_candidate"
    assert capabilities["get_shipping_options"]["mcp_exposure"] == "eligible_after_adapter_validation"
    assert all(item["mcp_exposure"] == "not_exposed" for key, item in capabilities.items() if key != "get_shipping_options")
