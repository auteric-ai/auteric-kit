"""Read-only adapter for an explicit local merchant catalog, never browser carts."""

import json
from pathlib import Path
from urllib.parse import urlsplit
from decimal import Decimal

from .connector import CommerceConnector
from .models import Product, ProductRequest, SearchRequest


def safe_path(root, relative):
    root = Path(root).absolute()
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Expected a repository-relative path")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlinked catalog/config paths are not supported")
    return path


class CatalogConnector(CommerceConnector):
    supported_operations = frozenset({"search_products", "get_product"})

    def __init__(self, root, path="agent-catalog.json"):
        self.path = safe_path(root, path)
        self.products()  # Fail before provisioning when the source is invalid.

    def products(self):
        if self.path.stat().st_size > 8_000_000:
            raise ValueError("Catalog exceeds 8 MB; use a paginated merchant API")
        data = json.loads(self.path.read_text())
        rows = data.get("products") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows or len(rows) > 10000:
            raise ValueError("Catalog requires 1–10000 products")
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Catalog products must be objects")
            identifier = row.get("id")
            if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
                raise ValueError("A stable product ID is required")
            identifier = str(identifier)
            if identifier in seen:
                raise ValueError("Duplicate catalog ID")
            seen.add(identifier)
            # A canonical SKU alias uses the stable ID when no merchant SKU exists.
            # Never invent stock, variants, product pages or prices.
            product = {key: row[key] for key in Product.model_fields if key in row and key != "metadata"}
            product.update(id=identifier, sku=str(row.get("sku") or identifier))
            if row.get("image") and not row.get("images"):
                product["images"] = [row["image"]]
            product["metadata"] = {"sku_source": "merchant" if row.get("sku") else "product_id_alias"}
            product = Product.model_validate(product).model_dump(mode="json")
            if product["currency"] not in {"USD", "EUR", "GBP", "ILS", "CAD", "AUD"}:
                raise ValueError("This UCP adapter needs an implemented currency exponent")
            amounts = [product["price"]] + [variant.get("price") for variant in product["variants"]]
            for amount in amounts:
                units = Decimal(str(amount)) * 100
                if not units.is_finite() or units < 0 or units != units.to_integral_value() or units > 9007199254740991:
                    raise ValueError("Catalog price cannot be represented in exact UCP minor units")
            for link in product["images"] + ([product["product_url"]] if product["product_url"] else []):
                url = urlsplit(link)
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError("Catalog links must be public HTTPS URLs without credentials")
            result.append(product)
        return result

    async def search_products(self, request):
        query = SearchRequest.model_validate(request)
        term = query.query.casefold()
        return [
            p
            for p in self.products()
            if term in (p["title"] + " " + (p.get("description") or "") + " " + p["sku"]).casefold()
        ][: query.limit]

    async def get_product(self, request):
        query = ProductRequest.model_validate(request)
        for product in self.products():
            if product["id"] == query.product_id:
                return product
        raise ValueError("Product not found")

    async def _unsupported(self, request):
        raise NotImplementedError("This catalog has no authenticated cart or checkout API")

    create_cart = _unsupported
    get_cart = _unsupported
    add_to_cart = _unsupported
    update_cart_item = _unsupported
    remove_from_cart = _unsupported
    create_checkout = _unsupported
    get_checkout = _unsupported
