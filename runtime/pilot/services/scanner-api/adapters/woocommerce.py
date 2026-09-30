from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from models import AdapterResult, Capability, Evidence, NormalizedProduct, NormalizedVariant
from .base import CommerceAdapter, ProgressCallback
from .generic_web import normalize_target


CATALOG_PAGE_SIZE = 100


class WooCommerceAdapter(CommerceAdapter):
    name = "woocommerce"

    async def scan(self, target_url: str, context: dict[str, Any], progress: ProgressCallback) -> AdapterResult:
        base = normalize_target(target_url).rstrip("/") + "/"
        connector_token = str(context.get("connector_token") or "")
        key = str(context.get("consumer_key") or "")
        secret = str(context.get("consumer_secret") or "")
        progress(15, "Reading WooCommerce catalog")
        async with httpx.AsyncClient(timeout=15.0) as client:
            catalog_meta: dict[str, Any]
            if connector_token:
                endpoint = urljoin(base, "/wp-json/ai-shopping-readiness/v1/snapshot")
                r = await client.get(endpoint, headers={"Accept": "application/json", "X-AISR-Token": connector_token})
                if r.status_code >= 400:
                    raise ValueError(f"WooCommerce connector returned HTTP {r.status_code}")
                snapshot = r.json()
                raw_products = snapshot.get("products") or []
                context = {**context, "store_name": snapshot.get("store_name"), "currency": snapshot.get("currency")}
                catalog_meta = {"source": "woocommerce_connector", "endpoint": endpoint, "products_discovered": len(raw_products) if isinstance(raw_products, list) else 0, "pages_fetched": 1, "complete": True, "capped": False}
            else:
                if not key or not secret:
                    raise ValueError("WooCommerce deep scan requires a connector token or read-only REST API credentials")
                endpoint = urljoin(base, "/wp-json/wc/v3/products")
                max_products = max(1, int(context.get("catalog_max_products") or 5000))
                raw_products = []
                pages = 0
                complete = False
                for page in range(1, (max_products + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE + 1):
                    r = await client.get(f"{endpoint}?per_page={CATALOG_PAGE_SIZE}&page={page}", auth=(key, secret), headers={"Accept": "application/json"})
                    if r.status_code >= 400:
                        if page > 1 and r.status_code == 400:
                            complete = True
                            break
                        raise ValueError(f"WooCommerce REST API returned HTTP {r.status_code}")
                    batch = r.json()
                    if not isinstance(batch, list):
                        break
                    raw_products.extend(x for x in batch if isinstance(x, dict))
                    pages += 1
                    if len(batch) < CATALOG_PAGE_SIZE:
                        complete = True
                        break
                    if len(raw_products) >= max_products:
                        break
                raw_products = raw_products[:max_products]
                catalog_meta = {"source": "woocommerce_rest_v3", "endpoint": endpoint, "products_discovered": len(raw_products), "pages_fetched": pages, "page_size": CATALOG_PAGE_SIZE, "complete": complete, "capped": bool(len(raw_products) >= max_products and not complete), "max_products": max_products}
        progress(48, "Normalizing WooCommerce products")
        products: list[NormalizedProduct] = []
        for p in raw_products if isinstance(raw_products, list) else []:
            attrs = p.get("attributes") or []
            variants = [
                NormalizedVariant(id=str(v), title="Variation")
                for v in (p.get("variations") or [])[:50]
            ]
            products.append(
                NormalizedProduct(
                    id=str(p.get("id")),
                    title=p.get("name") or "Untitled product",
                    description=BeautifulSoup(p.get("description") or p.get("short_description") or "", "html.parser").get_text(" ", strip=True) or None,
                    url=p.get("permalink") or base,
                    price=float(p.get("price")) if p.get("price") not in (None, "") else None,
                    currency=context.get("currency"),
                    brand=next((a.get("options", [None])[0] for a in attrs if str(a.get("name", "")).lower() == "brand" and a.get("options")), None),
                    sku=p.get("sku") or None,
                    availability="InStock" if p.get("stock_status") == "instock" else "OutOfStock",
                    images=[i.get("src") for i in p.get("images") or [] if isinstance(i, dict) and i.get("src")],
                    variants=variants,
                    categories=[(c.get("name") if isinstance(c, dict) else str(c)) for c in p.get("categories") or [] if (c.get("name") if isinstance(c, dict) else c)],
                    published=p.get("status") == "publish",
                    structured_data=False,
                    source="api",
                    source_confidence=1.0,
                )
            )
        progress(72, "Checking connected WooCommerce capabilities")
        return AdapterResult(
            adapter=self.name,
            platform="woocommerce",
            store_name=context.get("store_name"),
            target_url=base.rstrip("/"),
            products=products,
            capabilities={
                "connected_rest_api": Capability(supported=True, source="api", confidence=1.0, status="Verified Active"),
                "catalog_api": Capability(supported=True, source="api", confidence=1.0, status="Verified Active"),
                "checkout": Capability(supported=True, source="inferred", confidence=0.75, status="Available"),
                "runtime_protection": Capability(supported=False, source="inferred", confidence=0.4, status="Could Not Verify"),
            },
            observations={"connected": True, "api": "connector" if connector_token else "wc/v3", "catalog": catalog_meta},
            evidence=[Evidence(type="woocommerce_connector" if connector_token else "woocommerce_rest_v3", source="api", value={"products": len(products), "pages": catalog_meta.get("pages_fetched"), "complete": catalog_meta.get("complete")}, url=endpoint)],
        )
