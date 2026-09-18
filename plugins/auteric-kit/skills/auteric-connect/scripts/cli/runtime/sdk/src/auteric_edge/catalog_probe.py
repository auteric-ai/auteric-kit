"""Bounded read-only discovery of catalog contracts, never invented defaults."""
import re
from urllib.parse import quote, urlsplit

import httpx

from .mapping import lookup, map_fields, mapping_digest
from .models import validate_output


def collection(payload):
    for path in ("$", "items", "products", "data", "data.items", "data.products"):
        try:
            rows = lookup(payload, path)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if isinstance(rows, list):
            return path, rows
    raise ValueError("Catalog response has no supported product array")


def fields(sample):
    if not isinstance(sample, dict):
        raise ValueError("Product must be an object")
    result = {}
    for target, names in {
        "id": ("id", "product_id"), "sku": ("sku",),
        "title": ("title", "name"), "price": ("price",), "currency": ("currency",),
    }.items():
        source = next((name for name in names if sample.get(name) is not None), None)
        if source is None:
            raise ValueError(f"Product is missing {target}; an explicit adapter is required")
        result[target] = {"source": source}
    for target, names in {
        "description": ("description",), "availability": ("availability",),
        "images": ("images",), "variants": ("variants",),
        "product_url": ("product_url", "canonicalUrl"), "inventory": ("inventory",),
    }.items():
        source = next((name for name in names if name in sample), None)
        if source:
            result[target] = {"source": source}
    validate_output("get_product", map_fields(result, sample))
    return result


def discover(report, store_url, *, transport=None):
    if not store_url:
        report["connector_diagnostics"] = ["No local API origin supplied; configure an explicit connector for this deployment."]
        return None
    parsed = urlsplit(store_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Automatic local probing requires an explicit loopback origin")
    routes = {}
    for candidate in report.get("candidates", []):
        mapping = candidate.get("mapping") or {}
        if mapping.get("method") == "GET" and mapping.get("path"):
            routes.setdefault(candidate["operation"], set()).add(mapping["path"])
    pairs = [(search, detail) for search in sorted(routes.get("search_products", []))
             for detail in sorted(routes.get("get_product", []))
             if "{" not in search and re.fullmatch(re.escape(search.rstrip("/")) + r"/\{\w+\}", detail)]
    diagnostics = []
    if len(pairs) != 1:
        report["connector_diagnostics"] = [f"Found {len(pairs)} catalog route pairs; trace the authoritative API before creating a connector."]
        return None
    search, detail = pairs[0]
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False, transport=transport) as client:
        def get(path, params=None):
            response = client.get(store_url.rstrip("/") + path, params=params)
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("Catalog response exceeds the probe limit")
            return response.json()
        try:
            response_root, rows = collection(get(search, {"limit": 2}))
            if not rows:
                raise ValueError("Catalog is empty; supply a safe test product")
            product_fields = fields(rows[0])
            products = validate_output("search_products", [map_fields(product_fields, row) for row in rows])
            sample_id = products[0]["id"]
            if any(c in sample_id for c in "/\\%?#") or sample_id in {".", ".."}:
                raise ValueError("Product identifier requires an explicit path adapter")
            parameter = re.search(r"\{(\w+)\}", detail)[1]
            item = get(detail.replace("{" + parameter + "}", quote(sample_id, safe="")))
            item_fields = fields(item)
            if validate_output("get_product", map_fields(item_fields, item))["id"] != sample_id:
                raise ValueError("Lookup returned a different product")
            # Prove which query argument filters this endpoint. Unsupported query
            # arguments often return the entire catalog with a misleading HTTP 200.
            query_key = None
            for key in ("query", "q", "search"):
                _, negative = collection(get(search, {key: "auteric-probe-no-match-9af352cf"}))
                if negative:
                    continue
                _, positive = collection(get(search, {key: products[0]["title"]}))
                if any(map_fields(product_fields, row)["id"] == sample_id for row in positive):
                    query_key = key
                    break
            if query_key is None:
                raise ValueError("Search filtering could not be verified; configure its query contract")
            mappings = [
                {"operation": "search_products", "method": "GET", "path": search,
                 "request": {"query": {query_key: {"source": "query"}}},
                 "response_root": response_root, "response": product_fields},
                {"operation": "get_product", "method": "GET", "path": detail,
                 "request": {"path": {parameter: {"source": "product_id"}}}, "response": item_fields},
            ]
            return {"kind": "rest", "base_url": store_url.rstrip("/"), "allowed_paths": [search, detail],
                    "approved_mapping_digests": [mapping_digest(m) for m in mappings], "mappings": mappings,
                    "test_inputs": {"search_products": {"query": products[0]["title"], "limit": 2},
                                    "get_product": {"product_id": sample_id}},
                    "generated": {"kind": "local_read_only_catalog", "verified_url": store_url.rstrip("/") + search}}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Do not echo response bodies, headers, or merchant data into reports.
            diagnostics.append(f"Catalog contract probe failed ({type(exc).__name__}); check API availability, product schema, currency, lookup identity and search filtering.")
    report["connector_diagnostics"] = diagnostics
    return None
