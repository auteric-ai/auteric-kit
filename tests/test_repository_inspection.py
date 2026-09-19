import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "sdk" / "src"))
from auteric_edge.repository_inspection import inspect_repository  # noqa: E402


class RepositoryInspectionTests(unittest.TestCase):
    def test_inventories_every_api_but_selects_only_supported_shopping_tools(self):
        routes = [
            ("get", "/api/health"), ("get", "/api/meta/routes"),
            ("post", "/api/session/guest"), ("get", "/api/session"),
            ("post", "/api/auth/register"), ("post", "/api/auth/login"), ("post", "/api/auth/logout"),
            ("get", "/api/me"), ("patch", "/api/me"), ("get", "/api/me/addresses"),
            ("post", "/api/me/addresses"), ("delete", "/api/me/addresses/:addressId"),
            ("get", "/api/me/wishlist"), ("post", "/api/me/wishlist/:productId"),
            ("delete", "/api/me/wishlist/:productId"),
            ("get", "/api/products"), ("get", "/api/products/:id"),
            ("get", "/api/variants/:id"), ("get", "/api/categories"),
            ("get", "/api/shipping-methods"),
            ("post", "/api/carts"), ("get", "/api/carts"), ("get", "/api/carts/:id"),
            ("post", "/api/carts/:id/items"), ("patch", "/api/carts/:id/items/:lineId"),
            ("delete", "/api/carts/:id/items/:lineId"), ("put", "/api/carts/:id/items"),
            ("put", "/api/carts/:id/shipping"), ("put", "/api/carts/:id/coupon"),
            ("delete", "/api/carts/:id/coupon"), ("delete", "/api/carts/:id"),
            ("post", "/api/checkouts"), ("get", "/api/checkouts/:id"),
            ("patch", "/api/checkouts/:id"), ("post", "/api/checkouts/:id/lock"),
            ("delete", "/api/checkouts/:id"), ("post", "/api/checkouts/:id/payments"),
            ("get", "/api/payments/:id"), ("post", "/api/sandbox/payments/:id/complete"),
            ("post", "/api/webhooks/sandbox"),
            ("get", "/api/orders"), ("get", "/api/orders/:id"),
            ("get", "/api/orders/:id/tracking"), ("post", "/api/orders/:id/cancel"),
            ("post", "/api/orders/:id/refunds"),
            ("get", "/api/admin/products"), ("patch", "/api/admin/products/:id"),
            ("put", "/api/admin/inventory/:variantId"), ("put", "/api/admin/inventory/:productId/:size"),
            ("get", "/api/admin/orders"), ("patch", "/api/admin/orders/:id"),
            ("get", "/api/admin/returns"), ("get", "/api/admin/coupons"),
            ("post", "/api/admin/coupons"), ("delete", "/api/admin/coupons/:code"),
            ("get", "/api/admin/audit"), ("get", "/api/admin/stats"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = "\n".join(
                f"registerRoute('{method}', '{path}', {{}}, handler);" for method, path in routes[:-1]
            ) + f"\napp.{routes[-1][0]}('{routes[-1][1]}', handler);\n"
            (root / "server.js").write_text(source)
            report = inspect_repository(root)

        self.assertEqual(report["mode"], "full_api_inventory_filtered_commerce_tools")
        self.assertEqual(report["inventory_summary"]["total_endpoints"], len(routes))
        self.assertEqual(report["inventory_summary"]["api_endpoints"], len(routes))
        self.assertEqual(report["inventory_summary"]["tool_candidates"], 11)
        self.assertEqual({item["operation"] for item in report["candidates"]}, {
            "search_products", "get_product", "create_cart", "get_cart", "add_to_cart",
            "update_cart_item", "remove_from_cart", "replace_cart_items", "cancel_cart",
            "create_checkout", "get_checkout",
        })
        by_route = {item["route"]: item for item in report["api_inventory"]}
        self.assertEqual(by_route["/api/auth/login"]["exposure"], "internal_dependency")
        self.assertEqual(by_route["/api/admin/products"]["exposure"], "blocked_by_policy")
        self.assertEqual(by_route["/api/checkouts/:id/payments"]["exposure"], "blocked_by_policy")
        self.assertEqual(by_route["/api/webhooks/sandbox"]["exposure"], "blocked_by_policy")
        self.assertFalse(by_route["/api/carts/:id/coupon"]["tool_eligible"])
        self.assertFalse(by_route["/api/checkouts/:id/lock"]["tool_eligible"])

    def test_html_product_page_is_inventory_context_not_a_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "server.js").write_text(
                "app.get('/products/:id', (req, res) => { res.type('html').send(loadIndex()); });\n"
                "app.get('/api/products/:id', (req, res) => { res.json(product); });\n"
            )
            report = inspect_repository(root)
        by_route = {item["route"]: item for item in report["api_inventory"]}
        self.assertEqual(by_route["/products/:id"]["surface"], "storefront_page")
        self.assertFalse(by_route["/products/:id"]["tool_eligible"])
        self.assertTrue(by_route["/api/products/:id"]["tool_eligible"])

    def test_detects_literal_express_route_helper_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "server.js").write_text(
                "for (const base of ['/api/products']) {\n"
                "  route('get', base, null, handler);\n"
                "  route('get', base + '/:id', null, handler);\n"
                "}\n"
            )
            report = inspect_repository(root)
        detected = {(item["operation"], item["behavior"]) for item in report["candidates"]}
        self.assertIn(("search_products", "GET /api/products"), detected)
        self.assertIn(("get_product", "GET /api/products/:id"), detected)

    def test_detects_registered_catalog_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "server.js").write_text(
                "registerRoute('get', '/api/products', {}, handler);\n"
                "registerRoute('get', '/api/products/:id', {}, handler);\n"
            )
            report = inspect_repository(root)
        detected = {(item["operation"], item["behavior"]) for item in report["candidates"]}
        self.assertIn(("search_products", "GET /api/products"), detected)
        self.assertIn(("get_product", "GET /api/products/:id"), detected)

    def test_inventories_common_backend_route_syntaxes(self):
        sources = {
            "routes.php": "Route::get('/api/products', handler);",
            "routes.rb": "get '/api/products/:id', to: 'products#show'\n",
            "main.go": 'router.POST("/api/carts", handler)',
            "Products.java": '@GetMapping("/api/products/{id}")',
            "Products.cs": '[HttpGet("/api/products/{id}")]',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, content in sources.items():
                (root / name).write_text(content)
            report = inspect_repository(root)
        observed = {(item["method"], item["route"]) for item in report["api_inventory"]}
        self.assertIn(("GET", "/api/products"), observed)
        self.assertIn(("GET", "/api/products/:id"), observed)
        self.assertIn(("POST", "/api/carts"), observed)
        self.assertIn(("GET", "/api/products/{id}"), observed)


if __name__ == "__main__":
    unittest.main()
