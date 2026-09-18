import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "sdk" / "src"))
from auteric_edge.repository_inspection import inspect_repository  # noqa: E402


class RepositoryInspectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
