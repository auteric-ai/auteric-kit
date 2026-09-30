from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from models import AdapterResult, Capability, Evidence, NormalizedProduct, NormalizedVariant
from .base import CommerceAdapter, ProgressCallback


CATALOG_PAGE_SIZE = 250


def _catalog_limit() -> int:
    return max(1, int(os.getenv("SCANNER_CATALOG_MAX_PRODUCTS", "5000")))


class ShopifyAdapter(CommerceAdapter):
    """Connected Shopify read-only adapter.

    OAuth is intentionally kept outside the scanner core. The embedded Shopify shell
    should pass an authorized shop domain and token server-to-server. Raw tokens are
    used only for this request and are never placed into the persisted scan result.
    """

    name = "shopify"

    async def scan(self, target_url: str, context: dict[str, Any], progress: ProgressCallback) -> AdapterResult:
        shop = str(context.get("shop_domain") or urlparse(target_url).hostname or "").strip().lower()
        token = str(context.get("access_token") or "").strip()
        if not shop or not token:
            raise ValueError("Shopify connected scan requires an OAuth shop_domain and access_token")
        api_version = os.getenv("SHOPIFY_API_VERSION", "2026-07")
        endpoint = f"https://{shop}/admin/api/{api_version}/graphql.json"
        progress(15, "Reading Shopify catalog with least-privilege access")
        query = """
        query ScannerProducts($first:Int!, $after:String) {
          shop { name primaryDomain { url host } }
          products(first:$first, after:$after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id title descriptionHtml vendor productType status handle
              featuredMedia { preview { image { url } } }
              variants(first:50) { nodes { id title sku barcode price availableForSale } }
            }
          }
        }
        """
        max_products = _catalog_limit()
        pages = 0
        after = None
        raw_products: list[dict[str, Any]] = []
        shop_data: dict[str, Any] = {}
        complete = False
        async with httpx.AsyncClient(timeout=20.0) as client:
            while len(raw_products) < max_products:
                r = await client.post(
                    endpoint,
                    headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                    json={"query": query, "variables": {"first": min(CATALOG_PAGE_SIZE, max_products - len(raw_products)), "after": after}},
                )
                if r.status_code >= 400:
                    raise ValueError(f"Shopify Admin API returned HTTP {r.status_code}")
                body = r.json()
                if body.get("errors"):
                    raise ValueError("Shopify Admin API query failed")
                data = body.get("data") or {}
                shop_data = data.get("shop") or shop_data
                connection = data.get("products") or {}
                batch = connection.get("nodes") or []
                if not isinstance(batch, list):
                    break
                raw_products.extend(p for p in batch if isinstance(p, dict))
                pages += 1
                page_info = connection.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    complete = True
                    break
                after = page_info.get("endCursor")
                if not after or not batch:
                    break
        public_url = ((shop_data.get("primaryDomain") or {}).get("url") or target_url or f"https://{shop}").rstrip("/")
        progress(48, "Normalizing Shopify products and variants")
        products: list[NormalizedProduct] = []
        for p in raw_products:
            variants = []
            for v in ((p.get("variants") or {}).get("nodes") or []):
                variants.append(
                    NormalizedVariant(
                        id=str(v.get("id")),
                        title=v.get("title"),
                        sku=v.get("sku") or None,
                        price=float(v.get("price")) if v.get("price") is not None else None,
                        available=v.get("availableForSale"),
                    )
                )
            featured = (((p.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get("url")
            gtin = next((v.get("barcode") for v in ((p.get("variants") or {}).get("nodes") or []) if v.get("barcode")), None)
            products.append(
                NormalizedProduct(
                    id=str(p.get("id")),
                    title=p.get("title") or "Untitled product",
                    description=BeautifulSoup(p.get("descriptionHtml") or "", "html.parser").get_text(" ", strip=True) or None,
                    url=f"{public_url}/products/{p.get('handle')}" if p.get("handle") else public_url,
                    price=variants[0].price if variants else None,
                    brand=p.get("vendor") or None,
                    gtin=gtin,
                    sku=variants[0].sku if variants else None,
                    availability="InStock" if any(v.available for v in variants) else "OutOfStock",
                    images=[featured] if featured else [],
                    variants=variants,
                    categories=[p.get("productType")] if p.get("productType") else [],
                    published=p.get("status") == "ACTIVE",
                    structured_data=False,
                    source="api",
                    source_confidence=1.0,
                )
            )
        progress(72, "Combining Shopify API evidence with storefront readiness")
        return AdapterResult(
            adapter=self.name,
            platform="shopify",
            store_name=shop_data.get("name") or shop,
            target_url=public_url,
            products=products,
            capabilities={
                "connected_admin_api": Capability(supported=True, source="api", confidence=1.0, status="Verified Active"),
                "catalog_api": Capability(supported=True, source="api", confidence=1.0, status="Verified Active"),
                "checkout": Capability(supported=True, source="inferred", confidence=0.7, status="Available"),
                "runtime_protection": Capability(supported=False, source="inferred", confidence=0.4, status="Could Not Verify"),
            },
            observations={
                "shop_domain": shop, "api_version": api_version, "connected": True,
                "catalog": {"source": "shopify_admin_graphql", "endpoint": endpoint, "products_discovered": len(products), "pages_fetched": pages, "page_size": CATALOG_PAGE_SIZE, "complete": complete, "capped": bool(len(products) >= max_products and not complete), "max_products": max_products},
            },
            evidence=[Evidence(type="shopify_admin_graphql", source="api", value={"products": len(products), "pages": pages, "complete": complete, "api_version": api_version}, url=endpoint)],
        )
