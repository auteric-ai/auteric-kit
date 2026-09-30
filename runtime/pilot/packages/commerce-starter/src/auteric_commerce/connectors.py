import re
from decimal import Decimal
from typing import Protocol

import httpx

from .domain import DomainError, Settings, Variant


class Connector(Protocol):
    def get(self, variant_id: str) -> Variant: ...
    def set_price(self, variant: Variant, new_price: Decimal) -> None: ...


class Shopify:
    """Admin credentials belong only to this gateway, never to the agent."""
    def __init__(self, settings: Settings):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", settings.shop_domain or ""):
            raise ValueError("Use an exact myshopify.com hostname, not a URL")
        self.url = f"https://{settings.shop_domain}/admin/api/{settings.api_version}/graphql.json"
        self.token = settings.shopify_token.get_secret_value()

    def graphql(self, query, variables):
        try:
            response = httpx.post(self.url, headers={"X-Shopify-Access-Token": self.token},
                                  json={"query": query, "variables": variables}, timeout=15, follow_redirects=False)
            response.raise_for_status()
            body = response.json()
            if body.get("errors") or not body.get("data"):
                raise ValueError("GraphQL failure")
            return body["data"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise DomainError("Shopify request failed; inspect provider logs securely", 502) from exc

    def get(self, variant_id):
        if not re.fullmatch(r"gid://shopify/ProductVariant/[0-9]+", variant_id):
            raise DomainError("Expected a Shopify ProductVariant GID", 422)
        data = self.graphql('''query ReadVariant($id: ID!) {
          shop { currencyCode }
          productVariant(id: $id) { id title sku price inventoryQuantity product { id title } }
        }''', {"id": variant_id})
        row = data.get("productVariant")
        if not row:
            raise DomainError("Variant not found", 404)
        if row.get("inventoryQuantity") is None:
            raise DomainError("Inventory quantity unavailable; cannot fabricate a merchant listing", 422)
        return Variant(id=row["id"], product_id=row["product"]["id"],
                       title=f'{row["product"]["title"]} / {row["title"]}', sku=row.get("sku") or "",
                       price=row["price"], currency=data["shop"]["currencyCode"], stock=row["inventoryQuantity"])

    def set_price(self, variant, new_price):
        data = self.graphql('''mutation Price($product: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $product, variants: $variants, allowPartialUpdates: false) {
            productVariants { id price } userErrors { field message }
          }
        }''', {"product": variant.product_id, "variants": [{"id": variant.id, "price": format(new_price, ".2f")}]})
        result = data.get("productVariantsBulkUpdate") or {}
        if result.get("userErrors") or not any(v["id"] == variant.id and Decimal(v["price"]) == new_price for v in result.get("productVariants") or []):
            raise DomainError("Shopify did not confirm the expected price; reconciliation required", 502)
