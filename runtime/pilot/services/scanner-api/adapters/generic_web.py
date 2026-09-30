from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from models import AdapterResult, Capability, Evidence, NormalizedProduct, NormalizedVariant
from ucp_checks import analyze_ucp, probe_acp, probe_catalog, probe_references, probe_transports, validate_with_official_ucp_schema
from web_security import analyze_cookies, analyze_headers, analyze_page, analyze_robots, probe_cors, probe_http_redirect, probe_standard_files, probe_tls
from .base import CommerceAdapter, ProgressCallback

USER_AGENT = "AIShoppingReadinessScanner/0.1 (+merchant initiated scan)"
MAX_RESPONSE_BYTES = 2_500_000
MAX_REDIRECTS = 4
CATALOG_PAGE_SIZE = 250
# Shopify's public products endpoint may return several megabytes for image-rich
# catalogs. The initial report analyzes one bounded page; further pages are
# fetched and audited only when the merchant browses to them.
SHOPIFY_PUBLIC_PAGE_SIZE = max(1, min(50, int(os.getenv("SCANNER_SHOPIFY_PUBLIC_PAGE_SIZE", "50"))))
# Keep WooCommerce on the same predictable on-demand page size.
WOO_PUBLIC_PAGE_SIZE = max(1, min(50, int(os.getenv("SCANNER_WOO_PUBLIC_PAGE_SIZE", "50"))))
MAX_CATALOG_PRODUCTS = max(1, int(os.getenv("SCANNER_CATALOG_MAX_PRODUCTS", "5000")))


class UnsafeTarget(ValueError):
    pass


def normalize_target(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Store URL is required")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in {"http", "https"}:
        raise ValueError("Only http/https store URLs are supported")
    if not p.hostname:
        raise ValueError("Invalid store URL")
    if p.username or p.password:
        raise ValueError("Credentials in URLs are not allowed")
    # Strip fragments; keep path because merchants may scan a storefront subpath.
    return urlunparse((p.scheme, p.netloc, p.path or "/", "", p.query, ""))


def _is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(
        [
            addr.is_private,
            addr.is_loopback,
            addr.is_link_local,
            addr.is_multicast,
            addr.is_reserved,
            addr.is_unspecified,
        ]
    )


def _loopback_targets_allowed() -> bool:
    """Allow only loopback storefronts when the local launcher opts in."""
    return os.getenv("SCANNER_ALLOW_LOOPBACK_TARGETS", "").strip().lower() in {"1", "true", "yes"}


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def validate_public_url(url: str) -> None:
    p = urlparse(url)
    host = (p.hostname or "").lower().rstrip(".")
    allow_loopback = _loopback_targets_allowed()
    if _is_loopback_host(host) and allow_loopback:
        # A loopback literal is safe immediately. Named localhost targets are
        # resolved below as well so an unusual resolver configuration cannot
        # turn this development-only exception into a private-network proxy.
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    elif host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
        raise UnsafeTarget("Local/private hosts are not scannable from the public scanner")
    if host in {"169.254.169.254", "metadata.google.internal"}:
        raise UnsafeTarget("Cloud metadata endpoints are blocked")
    try:
        literal = ipaddress.ip_address(host)
        if _is_blocked_ip(str(literal)):
            raise UnsafeTarget("Private/reserved IP ranges are blocked")
        return
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM))
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve store host: {host}") from exc
    ips = {item[4][0] for item in infos}
    if not ips:
        raise ValueError(f"Could not resolve store host: {host}")
    if _is_loopback_host(host) and allow_loopback and all(ipaddress.ip_address(ip).is_loopback for ip in ips):
        return
    if any(_is_blocked_ip(ip) for ip in ips):
        raise UnsafeTarget("Store resolves to a private/reserved network address")


async def safe_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    accept: str = "text/html,*/*",
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    follow_redirects: bool = True,
) -> httpx.Response:
    method = method.upper()
    if method not in {"GET", "HEAD", "OPTIONS", "POST"}:
        raise ValueError("Unsafe scanner method")
    current = normalize_target(url)
    for _ in range(MAX_REDIRECTS + 1):
        from traffic_limit import request_limiter
        limiter = request_limiter.get()
        if limiter is not None:
            await limiter.wait()
        await validate_public_url(current)
        # Keep a consistent, descriptive client fingerprint and one cookie jar for the
        # whole scan. This is compatible with normal Cloudflare/Shopify edge delivery;
        # challenge pages are reported as protected instead of being circumvented.
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.8",
            **(headers or {}),
        }
        response = await client.request(method, current, headers=request_headers, json=json_body, follow_redirects=False)
        if follow_redirects and response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                return response
            current = urljoin(current, location)
            if response.status_code in {301, 302, 303} and method == "POST":
                method, json_body = "GET", None
            continue
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("Response is too large for the safe scanner")
        return response
    raise ValueError("Too many redirects")


async def safe_get(client: httpx.AsyncClient, url: str, *, accept: str = "text/html,*/*", follow_redirects: bool = True) -> httpx.Response:
    return await safe_request(client, "GET", url, accept=accept, follow_redirects=follow_redirects)


async def local_auteric_readiness(payload: Any, target_url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Read non-secret connector state only for an explicitly local scan."""
    target_host = (urlparse(target_url).hostname or "").lower().rstrip(".")
    if not _is_loopback_host(target_host) or not isinstance(payload, dict) or payload.get("auteric_local_test") is not True:
        return {"status": "not_applicable"}
    attestation = payload.get("auteric_attestation") or {}
    signed = attestation.get("payload") if isinstance(attestation, dict) else None
    endpoint = signed.get("endpoint") if isinstance(signed, dict) else None
    store_id = signed.get("store_id") if isinstance(signed, dict) else None
    parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
    if not parsed or parsed.scheme != "http" or not _is_loopback_host(parsed.hostname or "") or not isinstance(store_id, str):
        return {"status": "unavailable"}
    try:
        response = await safe_get(client, f"{parsed.scheme}://{parsed.netloc}/api/commerce/local-stores/{store_id}/readiness", accept="application/json,*/*")
        data = response.json() if response.status_code == 200 else {}
    except Exception:
        return {"status": "unavailable"}
    if not isinstance(data, dict) or data.get("store_id") != store_id:
        return {"status": "unavailable"}
    return {
        "status": "connected" if data.get("connected") else "incomplete",
        "catalog_url": f"{parsed.scheme}://{parsed.netloc}/api/commerce/local-stores/{store_id}/catalog",
        **data,
    }


async def local_auteric_catalog(connection: dict[str, Any], client: httpx.AsyncClient) -> list[NormalizedProduct]:
    """Use the approved local connector, never a shopper MCP credential, for catalog review."""
    if connection.get("status") != "connected" or not isinstance(connection.get("catalog_url"), str):
        return []
    try:
        response = await safe_get(client, connection["catalog_url"], accept="application/json,*/*")
        rows = response.json().get("products") if response.status_code == 200 else None
    except Exception:
        return []
    if not isinstance(rows, list) or len(rows) > 50:
        return []
    products = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("title"), str):
            return []
        variants = []
        for variant in row.get("variants") or []:
            if isinstance(variant, dict) and isinstance(variant.get("id"), str):
                variants.append(NormalizedVariant(
                    id=variant["id"], title=variant.get("title"), sku=variant.get("sku"),
                    price=_money(variant.get("price")), currency=row.get("currency"),
                    available=variant.get("availability") == "in_stock",
                    attributes=variant.get("attributes") if isinstance(variant.get("attributes"), dict) else {},
                ))
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        product_url = metadata.get("public_product_url")
        products.append(NormalizedProduct(
            id=row["id"], title=row["title"], description=row.get("description"),
            url=product_url if isinstance(product_url, str) and product_url.startswith(("http://", "https://")) else None,
            price=_money(row.get("price")), currency=row.get("currency"), sku=row.get("sku"),
            availability=row.get("availability"), images=_images(row.get("images")), variants=variants,
            categories=[metadata["category"]] if isinstance(metadata.get("category"), str) else [],
            structured_data=False, evidence_scope="auteric_local_connector", source="runtime", source_confidence=1.0,
        ))
    return products


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.,-]", "", str(value)).replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _images(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict) and value.get("url"):
        return [str(value["url"])]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("url"):
                out.append(str(item["url"]))
        return out
    return []


def _brand(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name") or value.get("brand")
    return None


def _offer_values(offers: Any) -> tuple[float | None, str | None, str | None]:
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if not isinstance(offers, dict):
        return None, None, None
    price = _money(offers.get("price") or offers.get("lowPrice"))
    currency = offers.get("priceCurrency")
    availability = offers.get("availability")
    if isinstance(availability, str) and "/" in availability:
        availability = availability.rsplit("/", 1)[-1]
    return price, currency, availability


def _iter_jsonld_nodes(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if graph:
            yield from _iter_jsonld_nodes(graph)


def _jsonld_products(soup: BeautifulSoup, page_url: str) -> list[NormalizedProduct]:
    products: list[NormalizedProduct] = []
    seen: set[str] = set()
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        text = script.string or script.get_text(" ", strip=True)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        for node in _iter_jsonld_nodes(data):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if not any(str(t).lower() == "product" for t in types if t):
                continue
            name = str(node.get("name") or "Untitled product")
            key = str(node.get("sku") or node.get("@id") or node.get("url") or name)
            if key in seen:
                continue
            seen.add(key)
            price, currency, availability = _offer_values(node.get("offers"))
            gtin = node.get("gtin") or node.get("gtin13") or node.get("gtin12") or node.get("gtin14") or node.get("mpn")
            products.append(
                NormalizedProduct(
                    id=key,
                    title=name,
                    description=node.get("description"),
                    url=urljoin(page_url, node.get("url") or page_url),
                    price=price,
                    currency=currency,
                    brand=_brand(node.get("brand")),
                    gtin=str(gtin) if gtin else None,
                    sku=str(node.get("sku")) if node.get("sku") else None,
                    availability=str(availability) if availability else None,
                    images=[urljoin(page_url, i) for i in _images(node.get("image"))],
                    categories=[str(node.get("category"))] if node.get("category") else [],
                    published=True,
                    structured_data=True,
                    source="external_scan",
                    source_confidence=0.82,
                )
            )
    return products



def _image_from_tag(tag: Any, page_url: str) -> str | None:
    if tag is None:
        return None
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = tag.get(attr) if hasattr(tag, "get") else None
        if isinstance(value, str) and value.strip() and not value.strip().startswith("data:image/svg"):
            return urljoin(page_url, value.strip())
    srcset = tag.get("srcset") if hasattr(tag, "get") else None
    if isinstance(srcset, str) and srcset.strip():
        candidates = [part.strip().split(" ", 1)[0] for part in srcset.split(",") if part.strip()]
        if candidates:
            return urljoin(page_url, candidates[-1])
    return None


def _html_product_cards(soup: BeautifulSoup, page_url: str, *, limit: int = 50) -> list[NormalizedProduct]:
    """Bounded storefront-card fallback for catalog context.

    It is intentionally conservative: only links that look like product-detail URLs are
    considered, and card-derived data is marked lower-confidence than JSON-LD/API data.
    """
    products: list[NormalizedProduct] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        low = href.lower()
        if not any(token in low for token in ("/products/", "/product/", "/shop/")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        # Avoid cart/search/category-only links that happen to contain the token.
        if not parsed.path or parsed.path.rstrip("/").count("/") < 2:
            continue
        key = parsed._replace(query="", fragment="").geturl()
        if key in seen:
            continue

        img = a.find("img")
        container = a
        for _ in range(3):
            if img is not None:
                break
            container = getattr(container, "parent", None)
            if container is None:
                break
            img = container.find("img") if hasattr(container, "find") else None
        image = _image_from_tag(img, page_url)

        title = (a.get("aria-label") or a.get("title") or "").strip()
        if not title and img is not None:
            title = str(img.get("alt") or "").strip()
        if not title:
            text = a.get_text(" ", strip=True)
            title = text[:160] if text else ""
        if not title and container is not None and hasattr(container, "find"):
            heading = container.find(["h2", "h3", "h4"])
            if heading:
                title = heading.get_text(" ", strip=True)[:160]
        if len(title) < 2:
            continue

        price = None
        currency = None
        card_text = ""
        price_container = a if a.select_one('.price, [itemprop="price"]') else container
        if price_container is not None and hasattr(price_container, "get_text"):
            price_node = price_container.select_one('.price, [itemprop="price"]')
            price_copy = BeautifulSoup(str(price_node or price_container), "html.parser")
            for old_price in price_copy.select('s, del, .compare-at-price'):
                old_price.decompose()
            card_text = price_copy.get_text(" ", strip=True)[:600]
        money_match = re.search(r"(?P<symbol>[$£€₪])\s?(?P<amount>\d{1,6}(?:[.,]\d{1,2})?)", card_text)
        if money_match:
            price = _money(money_match.group("amount"))
            currency = {"$": "USD", "£": "GBP", "€": "EUR", "₪": "ILS"}.get(money_match.group("symbol"))

        seen.add(key)
        products.append(
            NormalizedProduct(
                id=key,
                title=title,
                url=key,
                price=price,
                currency=currency,
                images=[image] if image else [],
                published=True,
                structured_data=False,
                evidence_scope="storefront_card",
                evidence_urls=[page_url],
                source="external_scan",
                source_confidence=0.48,
            )
        )
        if len(products) >= limit:
            break
    return products


def _sitemap_urls(xml: str) -> list[str]:
    """Extract sitemap locations without trusting their scheme or host yet."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [node.text.strip() for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'loc' and node.text and node.text.strip()]


def _sitemap_hints(robots_text: str, root: str) -> list[str]:
    hints = [urljoin(root, '/sitemap.xml')]
    for line in robots_text.splitlines():
        if line.lower().startswith('sitemap:'):
            value = line.split(':', 1)[1].strip()
            if value:
                hints.append(urljoin(root, value))
    return list(dict.fromkeys(hints))[:8]


async def _custom_sitemap_products(client: httpx.AsyncClient, root: str, robots_text: str) -> tuple[list[NormalizedProduct], dict[str, Any] | None]:
    """Read a bounded public sitemap sample for an otherwise unknown storefront.

    This is evidence collection, never a crawler bypass: every fetch uses the same
    declared scanner identity and public-request restrictions as the home page.
    """
    sitemap_urls: list[str] = []
    for hint in _sitemap_hints(robots_text, root):
        try:
            response = await safe_get(client, hint, accept='application/xml,text/xml,*/*')
            if response.status_code != 200:
                continue
            for value in _sitemap_urls(response.text):
                absolute = urljoin(hint, value)
                if urlparse(absolute).netloc == urlparse(root).netloc:
                    sitemap_urls.append(absolute)
        except Exception:
            continue
    sitemap_urls = list(dict.fromkeys(sitemap_urls))[:16]
    page_urls: list[str] = []
    for sitemap in sitemap_urls:
        try:
            response = await safe_get(client, sitemap, accept='application/xml,text/xml,*/*')
            if response.status_code != 200:
                continue
            for value in _sitemap_urls(response.text):
                absolute = urljoin(sitemap, value)
                parsed = urlparse(absolute)
                if parsed.netloc != urlparse(root).netloc:
                    continue
                path = parsed.path.lower()
                if any(token in path for token in ('/product/', '/products/', '/shop/', '/p/')):
                    page_urls.append(absolute)
        except Exception:
            continue
    page_urls = list(dict.fromkeys(page_urls))[:50]
    if not page_urls:
        return [], None
    semaphore = asyncio.Semaphore(5)
    async def read(url: str):
        async with semaphore:
            try:
                response = await safe_get(client, url)
                if response.status_code == 200 and 'text/html' in response.headers.get('content-type', ''):
                    return _jsonld_products(BeautifulSoup(response.text, 'html.parser'), str(response.url))
            except Exception:
                pass
            return []
    groups = await asyncio.gather(*(read(url) for url in page_urls))
    products = []
    seen = set()
    for group in groups:
        for product in group:
            key = product.url or product.id
            if key and key not in seen:
                seen.add(key)
                products.append(product)
    return products, {**_catalog_observation(source='custom_sitemap_jsonld', endpoint=sitemap_urls[0] if sitemap_urls else root,
                                              products=len(products), pages=len(page_urls), complete=False, page_size=50),
                      'products_scanned': len(products), 'sitemap_pages_considered': len(page_urls),
                      'total_known': False, 'discovery_method': 'public_sitemap_and_product_jsonld'}


async def _enrich_product_pages(client, products, root):
    """Read a bounded same-origin sample; never equate an unread page with absence."""
    semaphore = asyncio.Semaphore(5)
    async def enrich(product):
        if not product.url or urlparse(product.url).netloc != urlparse(root).netloc:
            return
        async with semaphore:
            try:
                response = await safe_get(client, product.url)
                if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
                    return
                soup = BeautifulSoup(response.text, "html.parser")
                product.evidence_scope = "product_page"
                product.evidence_urls.append(product.url)
                candidates = _jsonld_products(soup, product.url)
                matched = next((p for p in candidates if p.url == product.url), None)
                if matched:
                    for field in ("description", "price", "currency", "brand", "gtin", "sku", "availability", "images", "categories", "variants"):
                        value = getattr(matched, field)
                        if value is not None and value != [] and value != "":
                            setattr(product, field, value)
                    product.structured_data = True
                details = soup.select_one('.product-details, [itemtype="https://schema.org/Product"]')
                if details:
                    description = details.select_one('.product-description, [itemprop="description"]')
                    if description and not product.description:
                        product.description = description.get_text(" ", strip=True)
                    vendor = details.select_one('.vendor, [itemprop="brand"]')
                    if vendor and not product.brand:
                        product.brand = vendor.get_text(" ", strip=True)
                    if description and not product.categories:
                        product.categories = [a.get_text(" ", strip=True) for a in description.select('a[href^="/collections/"]')]
                    variants = details.select('.variant-picker input[name="id"]')
                    if variants and not product.variants:
                        product.variants = [NormalizedVariant(id=v.get("value", ""), title=v.parent.get_text(" ", strip=True), available=not v.has_attr("disabled")) for v in variants if v.get("value")]
                        product.availability = product.availability or ("InStock" if any(v.available for v in product.variants) else "OutOfStock")
            except Exception:
                return
    try:
        await asyncio.wait_for(asyncio.gather(*(enrich(p) for p in products[:50])), timeout=15)
    except asyncio.TimeoutError:
        pass  # Retain partial evidence; unread fields remain explicitly unverified.


def _merge_card_context(products: list[NormalizedProduct], cards: list[NormalizedProduct]) -> list[NormalizedProduct]:
    if not products:
        return cards
    by_url = {(p.url or "").rstrip("/").lower(): p for p in cards if p.url}
    by_title = {p.title.strip().lower(): p for p in cards if p.title}
    for product in products:
        match = None
        if product.url:
            match = by_url.get(product.url.rstrip("/").lower())
        if match is None and product.title:
            match = by_title.get(product.title.strip().lower())
        if not match:
            continue
        if not product.images and match.images:
            product.images = list(match.images)
        if product.price is None and match.price is not None:
            product.price = match.price
            product.currency = product.currency or match.currency
        if not product.url and match.url:
            product.url = match.url
    return products

def _detect_platform(html: str, headers: httpx.Headers) -> tuple[str, float]:
    lower = html.lower()
    h = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    if any(token in lower or token in h for token in ["cdn.shopify.com", "shopify.theme", "x-shopid", "myshopify.com"]):
        return "shopify", 0.9
    if any(token in lower or token in h for token in ["woocommerce", "wc-ajax", "/wp-content/plugins/woocommerce", "x-wp-"]):
        return "woocommerce", 0.9
    for name, tokens in {
        "wix": ("wixstatic.com", "x-wix-request-id"),
        "magento": ("x-magento-", "magento_ui", "/static/frontend/"),
        "bigcommerce": ("cdn11.bigcommerce.com", "stencil-utils", "bigcommerce.com"),
        "commercetools": ("commercetools.com", "commercetools"),
    }.items():
        if any(token in lower or token in h for token in tokens):
            return name, 0.8
    if "wp-content" in lower:
        return "wordpress", 0.65
    return "generic", 0.5


def _currency_hint(soup: BeautifulSoup, html: str) -> str | None:
    for selector, attr in [("meta[property='product:price:currency']", "content"), ("meta[property='og:price:currency']", "content")]:
        tag = soup.select_one(selector)
        if tag and tag.get(attr):
            code = str(tag.get(attr)).strip().upper()
            if re.match(r"^[A-Z]{3}$", code):
                return code
    patterns = [r'Shopify\.currency\.active\s*=\s*["\']([A-Z]{3})["\']', r'"currency"\s*:\s*"([A-Z]{3})"']
    for pattern in patterns:
        m = re.search(pattern, html, re.I)
        if m:
            return m.group(1).upper()
    return None


def _catalog_observation(
    *,
    source: str,
    endpoint: str,
    products: int,
    pages: int,
    complete: bool,
    capped: bool = False,
    page_size: int = CATALOG_PAGE_SIZE,
) -> dict[str, Any]:
    return {
        "source": source,
        "endpoint": endpoint,
        "products_discovered": products,
        "pages_fetched": pages,
        "page_size": page_size,
        "complete": complete,
        "capped": capped,
        "max_products": MAX_CATALOG_PRODUCTS,
    }


async def _shopify_public_products(client: httpx.AsyncClient, root: str, currency_hint: str | None = None) -> tuple[list[NormalizedProduct], dict[str, Any]]:
    endpoint = urljoin(root, "/products.json")
    raw_products: list[dict[str, Any]] = []
    catalog_total: int | None = None
    try:
        response, counted_total = await asyncio.gather(
            safe_get(client, f"{endpoint}?limit={SHOPIFY_PUBLIC_PAGE_SIZE}&page=1", accept="application/json"),
            _shopify_sitemap_product_count(client, root),
            return_exceptions=True,
        )
        if not isinstance(counted_total, Exception):
            catalog_total = counted_total
        if not isinstance(response, Exception) and response.status_code == 200 and "application/json" in response.headers.get("content-type", ""):
            data = response.json()
            batch = data.get("products") if isinstance(data, dict) else []
            if isinstance(batch, list):
                raw_products = [product for product in batch if isinstance(product, dict)][:SHOPIFY_PUBLIC_PAGE_SIZE]
    except Exception:
        # Keep any storefront evidence already collected and mark the total unknown.
        pass

    products = _normalize_shopify_products(raw_products, root, currency_hint)
    total_known = catalog_total is not None
    public_total = max(catalog_total or 0, len(products)) if total_known else len(products)
    complete = bool(public_total <= len(products)) if total_known else len(raw_products) < SHOPIFY_PUBLIC_PAGE_SIZE
    return products, {
        **_catalog_observation(
            source="shopify_public_api", endpoint=endpoint, products=public_total, pages=1 if raw_products else 0,
            complete=complete, capped=False, page_size=SHOPIFY_PUBLIC_PAGE_SIZE,
        ),
        "products_scanned": len(products),
        "total_products": public_total,
        "total_known": total_known,
        "total_source": "shopify_product_sitemap" if total_known else None,
        "page_count": ((public_total + SHOPIFY_PUBLIC_PAGE_SIZE - 1) // SHOPIFY_PUBLIC_PAGE_SIZE) if public_total else 0,
        "lazy_pagination": bool(total_known and public_total > len(products)),
        "currency_hint": currency_hint,
    }


def _normalize_shopify_products(raw_products: list[dict[str, Any]], root: str, currency_hint: str | None = None) -> list[NormalizedProduct]:
    products: list[NormalizedProduct] = []
    for p in raw_products:
        variants = []
        raw_variants = p.get("variants") or []
        for v in raw_variants:
            variants.append(
                NormalizedVariant(
                    id=str(v.get("id") or v.get("sku") or "variant"),
                    title=v.get("title"),
                    sku=v.get("sku"),
                    price=_money(v.get("price")),
                    currency=currency_hint,
                    available=v.get("available") if isinstance(v.get("available"), bool) else None,
                    attributes={"option1": v.get("option1"), "option2": v.get("option2"), "option3": v.get("option3")},
                )
            )
        images = [i.get("src") for i in p.get("images") or [] if isinstance(i, dict) and i.get("src")]
        price = variants[0].price if variants else None
        availability = "InStock" if any(v.available is True for v in variants) else ("OutOfStock" if variants and all(v.available is False for v in variants) else None)
        barcode = next((str(v.get("barcode")) for v in raw_variants if v.get("barcode")), None)
        products.append(
            NormalizedProduct(
                id=str(p.get("id") or p.get("handle") or p.get("title")),
                title=p.get("title") or "Untitled product",
                description=BeautifulSoup(p.get("body_html") or "", "html.parser").get_text(" ", strip=True) or None,
                url=urljoin(root, f"/products/{p.get('handle')}") if p.get("handle") else root,
                price=price,
                currency=currency_hint,
                brand=p.get("vendor") or None,
                gtin=barcode,
                sku=(variants[0].sku if variants else None),
                availability=availability,
                images=images,
                variants=variants,
                categories=[p.get("product_type")] if p.get("product_type") else [],
                published=True,
                structured_data=False,
                source="external_scan",
                source_confidence=0.72,
            )
        )
    return products


async def _shopify_sitemap_product_count(client: httpx.AsyncClient, root: str) -> int | None:
    """Count discoverable Shopify products without downloading their full JSON records."""
    response = await safe_get(client, urljoin(root, "/sitemap.xml"), accept="application/xml,text/xml,*/*")
    if response.status_code != 200:
        return None
    try:
        document = ET.fromstring(response.content)
    except ET.ParseError:
        return None
    product_maps: list[str] = []
    for node in document.iter():
        if not str(node.tag).endswith("loc") or not node.text:
            continue
        candidate = node.text.strip()
        path = urlparse(candidate).path
        if path.startswith("/sitemap_products_") and path.endswith(".xml"):
            product_maps.append(candidate)
    if not product_maps:
        return None
    responses = await asyncio.gather(
        *(safe_get(client, url, accept="application/xml,text/xml,*/*") for url in product_maps),
        return_exceptions=True,
    )
    total = 0
    for child in responses:
        if isinstance(child, Exception) or child.status_code != 200:
            return None
        try:
            child_document = ET.fromstring(child.content)
        except ET.ParseError:
            return None
        total += sum(1 for node in child_document.iter() if str(node.tag).endswith("url"))
    return total


async def fetch_public_catalog_page(
    target_url: str,
    platform: str,
    page: int,
    page_size: int = 50,
    currency_hint: str | None = None,
) -> list[NormalizedProduct]:
    """Fetch exactly one public catalog page for on-demand browser pagination."""
    target = normalize_target(target_url)
    await validate_public_url(target)
    root = f"{urlparse(target).scheme}://{urlparse(target).netloc}/"
    page = max(1, int(page))
    page_size = max(1, min(50, int(page_size)))
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if platform == "shopify":
            endpoint = urljoin(root, "/products.json")
            response = await safe_get(client, f"{endpoint}?limit={page_size}&page={page}", accept="application/json")
            if response.status_code != 200 or "application/json" not in response.headers.get("content-type", ""):
                raise ValueError(f"Catalog page returned HTTP {response.status_code}")
            data = response.json()
            raw = data.get("products") if isinstance(data, dict) else []
            if not isinstance(raw, list):
                raise ValueError("Catalog page did not return a product list")
            return _normalize_shopify_products([item for item in raw if isinstance(item, dict)], root, currency_hint)
        if platform == "woocommerce":
            endpoint = urljoin(root, "/wp-json/wc/store/v1/products")
            response = await safe_get(client, f"{endpoint}?per_page={page_size}&page={page}", accept="application/json")
            if response.status_code != 200:
                raise ValueError(f"Catalog page returned HTTP {response.status_code}")
            raw = response.json()
            if not isinstance(raw, list):
                raise ValueError("Catalog page did not return a product list")
            return _normalize_woo_products(raw, root)
    raise ValueError("On-demand catalog pagination is unavailable for this storefront")


async def _woo_public_products(client: httpx.AsyncClient, root: str) -> tuple[list[NormalizedProduct], dict[str, Any]]:
    endpoint = urljoin(root, "/wp-json/wc/store/v1/products")
    raw_products: list[dict[str, Any]] = []
    catalog_total: int | None = None
    try:
        response = await safe_get(client, f"{endpoint}?per_page={WOO_PUBLIC_PAGE_SIZE}&page=1", accept="application/json")
        if response.status_code == 200:
            batch = response.json()
            if isinstance(batch, list):
                raw_products = [product for product in batch if isinstance(product, dict)][:WOO_PUBLIC_PAGE_SIZE]
                try:
                    catalog_total = max(0, int(response.headers.get("x-wp-total", "")))
                except ValueError:
                    catalog_total = None
    except Exception:
        pass
    products = _normalize_woo_products(raw_products, root)
    total_known = catalog_total is not None
    public_total = max(catalog_total or 0, len(products)) if total_known else len(products)
    complete = bool(public_total <= len(products)) if total_known else len(raw_products) < WOO_PUBLIC_PAGE_SIZE
    return products, {
        **_catalog_observation(
            source="woocommerce_store_api", endpoint=endpoint, products=public_total, pages=1 if raw_products else 0,
            complete=complete, capped=False, page_size=WOO_PUBLIC_PAGE_SIZE,
        ),
        "products_scanned": len(products),
        "total_products": public_total,
        "total_known": total_known,
        "total_source": "woocommerce_x_wp_total" if total_known else None,
        "page_count": ((public_total + WOO_PUBLIC_PAGE_SIZE - 1) // WOO_PUBLIC_PAGE_SIZE) if public_total else 0,
        "lazy_pagination": bool(total_known and public_total > len(products)),
    }


def _normalize_woo_products(raw_products: list[dict[str, Any]], root: str) -> list[NormalizedProduct]:
    products: list[NormalizedProduct] = []
    for p in raw_products:
        prices = p.get("prices") or {}
        decimals = int(prices.get("currency_minor_unit") or 2)
        raw_price = prices.get("price")
        try:
            price = int(raw_price) / (10**decimals) if raw_price is not None else None
        except Exception:
            price = _money(raw_price)
        images = [i.get("src") for i in p.get("images") or [] if isinstance(i, dict) and i.get("src")]
        products.append(
            NormalizedProduct(
                id=str(p.get("id") or p.get("slug") or p.get("name")),
                title=p.get("name") or "Untitled product",
                description=BeautifulSoup(p.get("description") or p.get("short_description") or "", "html.parser").get_text(" ", strip=True) or None,
                url=p.get("permalink") or root,
                price=price,
                currency=prices.get("currency_code"),
                sku=p.get("sku") or None,
                availability="InStock" if p.get("is_in_stock") else "OutOfStock",
                images=images,
                categories=[c.get("name") for c in p.get("categories") or [] if isinstance(c, dict) and c.get("name")],
                published=True,
                structured_data=False,
                source="external_scan",
                source_confidence=0.82,
            )
        )
    return products


def _cloudflare_observation(response: httpx.Response) -> dict[str, Any]:
    headers = response.headers
    server = headers.get("server", "").lower()
    detected = "cloudflare" in server or bool(headers.get("cf-ray"))
    return {
        "detected": detected,
        "ray": headers.get("cf-ray"),
        "cache_status": headers.get("cf-cache-status"),
        "challenge": bool(detected and (response.status_code in {403, 429, 503} or headers.get("cf-mitigated"))),
        "http_status": response.status_code,
    }


class GenericWebAdapter(CommerceAdapter):
    name = "generic"

    async def scan(self, target_url: str, context: dict[str, Any], progress: ProgressCallback) -> AdapterResult:
        target = normalize_target(target_url)
        await validate_public_url(target)
        progress(12, "Connecting to storefront")
        timeout = httpx.Timeout(12.0, connect=6.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            page = await safe_get(client, target)
            cloudflare = _cloudflare_observation(page)
            if cloudflare["challenge"]:
                # Do not evade a merchant's bot protection. We still record a
                # useful bounded observation and try public discovery files with
                # the same declared identity; a challenge is neither a merchant
                # defect nor proof that the merchant has no catalog.
                root = f"{urlparse(str(page.url)).scheme}://{urlparse(str(page.url)).netloc}/"
                robots, ucp = await asyncio.gather(
                    safe_get(client, urljoin(root, '/robots.txt'), accept='text/plain,*/*'),
                    safe_get(client, urljoin(root, '/.well-known/ucp'), accept='application/json,*/*'),
                    return_exceptions=True,
                )
                robots_status = robots.status_code if isinstance(robots, httpx.Response) else None
                robots_text = robots.text[:100_000] if isinstance(robots, httpx.Response) and robots.status_code == 200 else ''
                ucp_status = 'verified' if isinstance(ucp, httpx.Response) and ucp.status_code == 200 else ('protected' if isinstance(ucp, httpx.Response) and ucp.status_code in {401, 403} else 'not_found')
                title = urlparse(str(page.url)).hostname or urlparse(target).hostname or 'Storefront'
                progress(78, 'Storefront protection prevented a public catalog read')
                return AdapterResult(
                    adapter=self.name, platform='generic', store_name=title, target_url=str(page.url), products=[],
                    capabilities={
                        'public_storefront': Capability(supported=False, source='external_scan', confidence=1.0, status='Blocked by bot protection'),
                        'structured_product_data': Capability(supported=False, source='external_scan', confidence=0.0, status='Could Not Verify'),
                        'public_product_api': Capability(supported=False, source='external_scan', confidence=0.0, status='Could Not Verify'),
                        'ucp': Capability(supported=ucp_status == 'verified', source='external_scan', confidence=.8, status='Verified Active' if ucp_status == 'verified' else 'Could Not Verify'),
                        'runtime_protection': Capability(supported=False, source='external_scan', confidence=.0, status='Could Not Verify'),
                    },
                    observations={
                        'platform_confidence': .0, 'http_status': page.status_code, 'https': urlparse(str(page.url)).scheme == 'https',
                        'cloudflare': cloudflare, 'robots_status': robots_status, 'robots_present': bool(robots_text),
                        'ai_bot_access': {k: bool(v.get('allowed')) for k, v in analyze_robots(robots_text).get('bot_access', {}).items()} if robots_text else {},
                        'ucp_status': ucp_status, 'ucp_http_status': ucp.status_code if isinstance(ucp, httpx.Response) else None,
                        'catalog': {**_catalog_observation(source='bot_protection_blocked', endpoint=str(page.url), products=0, pages=0, complete=False),
                                    'blocked': True, 'blocker': 'cloudflare_challenge', 'total_known': False,
                                    'next_step': 'Use a merchant-authorized feed, API or Auteric Skill connection.'},
                    },
                    evidence=[Evidence(type='bot_protection', source='external_scan', value=cloudflare, url=str(page.url)),
                              Evidence(type='robots', source='external_scan', value={'status': robots_status}, url=urljoin(root, '/robots.txt'))],
                )
            if page.status_code >= 400:
                raise ValueError(f"Storefront returned HTTP {page.status_code}")
            content_type = page.headers.get("content-type", "")
            if "text/html" not in content_type:
                raise ValueError("Target did not return an HTML storefront")
            html = page.text
            effective_url = str(page.url)
            latency_ms = round(page.elapsed.total_seconds() * 1000) if page.elapsed else None
            http_status = page.status_code
            soup = BeautifulSoup(html, "html.parser")
            title = (soup.title.string.strip() if soup.title and soup.title.string else urlparse(effective_url).hostname)
            platform, platform_conf = _detect_platform(html, page.headers)
            progress(28, f"Detected {platform.title()} storefront")

            products = _jsonld_products(soup, effective_url)
            card_products = _html_product_cards(soup, effective_url)
            catalog = _catalog_observation(source="storefront_evidence", endpoint=effective_url, products=len(products), pages=1, complete=False)
            root = f"{urlparse(effective_url).scheme}://{urlparse(effective_url).netloc}/"
            currency_hint = _currency_hint(soup, html)
            if platform == "shopify":
                api_products, api_catalog = await _shopify_public_products(client, root, currency_hint)
                if api_products:
                    products = api_products
                    catalog = api_catalog
            elif platform == "woocommerce":
                api_products, api_catalog = await _woo_public_products(client, root)
                if api_products:
                    products = api_products
                    catalog = api_catalog
            products = _merge_card_context(products, card_products)
            if catalog.get("source") == "storefront_evidence":
                catalog["products_discovered"] = len(products)
                await _enrich_product_pages(client, products, root)
                catalog["detail_pages_attempted"] = min(50, len(products))
            progress(52, f"Found {len(products)} publicly visible products with {sum(1 for p in products if p.images)} images")

            robots_text = ""
            robots_status = None
            try:
                robots = await safe_get(client, urljoin(root, "/robots.txt"), accept="text/plain,*/*")
                robots_status = robots.status_code
                robots_text = robots.text[:100_000] if robots.status_code == 200 else ""
            except Exception:
                pass
            robots_analysis = analyze_robots(robots_text) if robots_text else {"bot_access": {}, "blocks_all": False}

            # Unknown storefronts often expose their product pages only through a
            # sitemap. Treat this as a bounded, public catalog sample rather than
            # assuming that a missing home-page product card means no catalog.
            if platform == 'generic' and not products and not robots_analysis.get('blocks_all'):
                sitemap_products, sitemap_catalog = await _custom_sitemap_products(client, root, robots_text)
                if sitemap_products:
                    products = sitemap_products
                    catalog = sitemap_catalog or catalog

            ucp_status = "not_found"
            ucp_payload: Any = None
            ucp_content_type = None
            ucp_http_status = None
            profile_url = urljoin(root, "/.well-known/ucp")
            try:
                ucp = await safe_get(client, profile_url, accept="application/json,*/*")
                ucp_content_type = ucp.headers.get("content-type")
                ucp_http_status = ucp.status_code
                if ucp.status_code == 200:
                    try:
                        ucp_payload = ucp.json()
                        ucp_status = "verified" if isinstance(ucp_payload, dict) else "invalid"
                    except Exception:
                        ucp_status = "invalid"
                        ucp_payload = None
                elif ucp.status_code in {401, 403}:
                    ucp_status = "protected"
            except Exception:
                ucp_status = "unknown"

            progress(62, "Validating UCP, discovery and trust signals")
            ucp_analysis = analyze_ucp(ucp_payload, profile_url) if ucp_payload else analyze_ucp(None, profile_url)
            ucp_analysis["content_type"] = ucp_content_type
            ucp_analysis["content_type_json"] = bool(ucp_content_type and "json" in ucp_content_type.lower())
            official_validation, reference_probes, transport_probes, catalog_probe, acp_analysis = await asyncio.gather(
                validate_with_official_ucp_schema(ucp_payload) if ucp_payload else asyncio.sleep(0, result={"engine": "ucp-schema", "available": False, "ran": False, "valid": None, "errors": []}),
                probe_references(ucp_analysis, safe_get, client) if ucp_payload else asyncio.sleep(0, result=[]),
                probe_transports(ucp_analysis, safe_request, client) if ucp_payload else asyncio.sleep(0, result=[]),
                probe_catalog(
                    ucp_analysis, safe_request, client, agent_profile_url=os.getenv("SCANNER_AGENT_PROFILE_URL", "").strip() or (os.getenv("AUTERIC_PUBLIC_URL", "").rstrip("/") + "/.well-known/ucp-agent" if os.getenv("AUTERIC_PUBLIC_URL", "").startswith("https://") else None),
                ) if ucp_payload else asyncio.sleep(0, result={"attempted": False, "reason": "UCP profile unavailable"}),
                probe_acp(root, safe_request, client, page_html=html),
            )
            ucp_analysis["official_schema_validation"] = official_validation
            ucp_analysis["reference_probes"] = reference_probes
            ucp_analysis["transport_probes"] = transport_probes
            ucp_analysis["catalog_probe"] = catalog_probe
            ucp_analysis["broken_reference_count"] = sum(1 for p in ucp_analysis["reference_probes"] if not p.get("ok"))
            ucp_analysis["broken_transport_count"] = sum(1 for p in ucp_analysis["transport_probes"] if not p.get("reachable"))
            local_connection = await local_auteric_readiness(ucp_payload, effective_url, client)
            local_products = await local_auteric_catalog(local_connection, client)
            if local_products:
                products = local_products
                catalog = _catalog_observation(
                    source="auteric_local_connector", endpoint="local tested connector",
                    products=len(products), pages=1, complete=True,
                )
                catalog["total_products"] = len(products)

            progress(70, "Running safe web-security probes")
            https = urlparse(effective_url).scheme == "https"
            header_analysis = analyze_headers(page.headers, https)
            cookie_analysis = analyze_cookies(page.headers)
            page_analysis = analyze_page(html)
            standard_files = await probe_standard_files(client, root, safe_get)
            cors_home = await probe_cors(client, effective_url, safe_request)
            cors_ucp = await probe_cors(client, profile_url, safe_request) if ucp_status in {"verified", "invalid", "protected"} else {"status": None}
            http_redirect = await probe_http_redirect(client, effective_url, safe_request)
            tls = await probe_tls(effective_url)

            header_checks = header_analysis["controls"]
            ai_bot_allowed = {k: bool(v.get("allowed")) for k, v in robots_analysis.get("bot_access", {}).items()}
            capabilities = {
                "public_storefront": Capability(supported=True, source="external_scan", confidence=1.0, status="Verified Active"),
                "structured_product_data": Capability(supported=any(p.structured_data for p in products), source="external_scan", confidence=0.9, status="Verified Active" if any(p.structured_data for p in products) else "Could Not Verify"),
                "public_product_api": Capability(supported=bool(products) and platform in {"shopify", "woocommerce"}, source="external_scan", confidence=0.75, status="Available" if bool(products) and platform in {"shopify", "woocommerce"} else "Could Not Verify"),
                "ucp": Capability(supported=ucp_status == "verified", source="external_scan", confidence=0.98 if ucp_status == "verified" else 0.8, status="Verified Active" if ucp_status == "verified" else ("Invalid" if ucp_status == "invalid" else "Could Not Verify")),
                "ucp_checkout": Capability(supported=bool(ucp_analysis.get("checkout_capability")), source="external_scan", confidence=0.96 if ucp_payload else 0.4, status="Verified Active" if ucp_analysis.get("checkout_capability") else "Could Not Verify"),
                "ucp_transports": Capability(supported=bool(ucp_analysis.get("transports")), source="external_scan", confidence=0.95 if ucp_payload else 0.4, status=", ".join(t.upper() for t in ucp_analysis.get("transports", [])) or "Could Not Verify"),
                "checkout": Capability(supported=bool(ucp_analysis.get("checkout_capability")), source="external_scan", confidence=0.75 if ucp_analysis.get("checkout_capability") else 0.35, status="Declared in UCP" if ucp_analysis.get("checkout_capability") else "Could Not Verify"),
                "runtime_protection": Capability(supported=False, source="external_scan", confidence=0.3, status="Could Not Verify"),
            }
            evidence = [
                Evidence(type="platform_detection", source="external_scan", value={"platform": platform, "confidence": platform_conf}, url=effective_url),
                Evidence(type="http_status", source="external_scan", value=page.status_code, url=effective_url),
                Evidence(type="https", source="external_scan", value=https, url=effective_url),
                Evidence(type="robots", source="external_scan", value={"status": robots_status, **robots_analysis}, url=urljoin(root, "/robots.txt")),
                Evidence(type="ucp_analysis", source="external_scan", value=ucp_analysis, url=profile_url),
                Evidence(type="web_security", source="external_scan", value={"headers": header_analysis, "cookies": cookie_analysis, "cors": {"home": cors_home, "ucp": cors_ucp}, "tls": tls}, url=effective_url),
            ]
            progress(78, "Building evidence-backed readiness report")
            return AdapterResult(
                adapter=self.name,
                platform=platform,
                store_name=title,
                target_url=effective_url,
                products=products,
                capabilities=capabilities,
                observations={
                    "platform_confidence": platform_conf,
                    "http_status": http_status,
                    "latency_ms": latency_ms,
                    "ucp_http_status": ucp_http_status,
                    "https": https,
                    "security_headers": header_checks,
                    "security_header_analysis": header_analysis,
                    "cookie_security": cookie_analysis,
                    "cors": {"home": cors_home, "ucp": cors_ucp},
                    "tls": tls,
                    "http_redirect": http_redirect,
                    "page_signals": page_analysis,
                    "standard_files": standard_files,
                    "robots_status": robots_status,
                    "robots_present": bool(robots_text),
                    "robots_blocks_all": bool(robots_analysis.get("blocks_all")),
                    "ai_bot_access": ai_bot_allowed,
                    "ucp_status": ucp_status,
                    "ucp_profile": ucp_payload,
                    "ucp_analysis": ucp_analysis,
                    "local_auteric": local_connection,
                    "acp_analysis": acp_analysis,
                    "page_title": title,
                    "catalog": catalog,
                    "cloudflare": cloudflare,
                },
                evidence=evidence,
            )
