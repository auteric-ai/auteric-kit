import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from auteric_edge.catalog_probe import discover
from auteric_edge.mapping import MappedConnector
from auteric_edge.repository_inspection import inspect_repository


class FreshStoreMatrix(unittest.TestCase):
    def scenario(self, syntax, wrapper, query_key, *, currency=True, lookup_id="one", filters=True):
        source = {
            "express": ("server.js", "app.get('/api/products', handler); app.get('/api/products/:id', handler);"),
            "helper": ("server.js", "registerRoute('get', '/api/products', {}, handler); registerRoute('get', '/api/products/:id', {}, handler);"),
            "fastapi": ("main.py", "@app.get('/api/products')\ndef products(): pass\n@app.get('/api/products/{id}')\ndef product(id): pass\n"),
            "flask": ("app.py", "@app.route('/api/products', methods=['GET'])\ndef products(): pass\n@app.route('/api/products/<string:id>', methods=['GET'])\ndef product(id): pass\n"),
        }[syntax]
        product = {"id": "one", "sku": "SKU-1", "title": "Test shoe", "price": "12.50", "currency": "EUR", "images": ["https://shop.example/shoe.jpg"]}
        if not currency:
            del product["currency"]
        def wrap(rows):
            if wrapper == "$": return rows
            if wrapper == "data.items": return {"data": {"items": rows}}
            return {wrapper: rows}
        def handle(request):
            if request.url.path.endswith('/one'):
                return httpx.Response(200, json={**product, "id": lookup_id})
            query = request.url.params.get(query_key, '')
            rows = [] if filters and query and query != product['title'] else [product]
            return httpx.Response(200, json=wrap(rows))
        transport = httpx.MockTransport(handle)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / source[0]).write_text(source[1])
            report = inspect_repository(root)
            config = discover(report, 'http://127.0.0.1:5173', transport=transport)
        return config, report, transport

    def test_fresh_repositories_and_simulated_mapping_contracts(self):
        for syntax in ('express', 'helper', 'fastapi', 'flask'):
            for wrapper in ('$', 'items', 'products', 'data.items'):
                for key in ('q', 'query', 'search'):
                    with self.subTest(syntax=syntax, wrapper=wrapper, query=key):
                        config, _, transport = self.scenario(syntax, wrapper, key)
                        self.assertIsNotNone(config)
                        async def check():
                            impl = MappedConnector(config['base_url'], allowed_paths=config['allowed_paths'], approved_mapping_digests=config['approved_mapping_digests'], allow_loopback=True, transport=transport)
                            try:
                                for mapping in config['mappings']:
                                    op = mapping['operation']
                                    result = await impl.execute(op, config['test_inputs'][op], mapping)
                                    product = result[0] if isinstance(result, list) else result
                                    self.assertEqual(product['currency'], 'EUR')
                                    self.assertEqual(product['id'], 'one')
                            finally:
                                await impl.close()
                        asyncio.run(check())

    def test_unknown_currency_wrong_lookup_and_ignored_filter_fail_closed(self):
        for options in ({'currency': False}, {'lookup_id': 'other'}, {'filters': False}):
            with self.subTest(options=options):
                config, report, _ = self.scenario('helper', 'items', 'query', **options)
                self.assertIsNone(config)
                self.assertTrue(report['connector_diagnostics'])

    def test_missing_routes_and_nonlocal_probe_are_explicit(self):
        report = {'candidates': []}
        self.assertIsNone(discover(report, 'http://127.0.0.1:5173'))
        self.assertIn('0 catalog route pairs', report['connector_diagnostics'][0])
        with self.assertRaises(ValueError):
            discover(report, 'http://example.com')

    def test_authentication_error_and_spa_html_do_not_create_connectors(self):
        _, report, _ = self.scenario('express', 'items', 'query')
        for status, body in ((401, 'Unauthorized'), (200, '<html>Storefront</html>'), (503, 'Unavailable')):
            with self.subTest(status=status):
                transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
                self.assertIsNone(discover(report, 'http://127.0.0.1:5173', transport=transport))
                self.assertTrue(report['connector_diagnostics'])
