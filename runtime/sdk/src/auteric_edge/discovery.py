"""Pure public-page evidence extraction. Candidates are never executable connectors."""

import hashlib
import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


def public_url(value):
    """Normalize a report URL, not a network/SSRF validator."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Only credential-free HTTPS origins are supported")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", ""))


def catalog_response_candidate(source_url, endpoint, body):
    """Observe product-shaped JSON from an actual page GET; never infer write APIs."""
    if len(body) > 1_000_000:
        return None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    container = "root"
    if isinstance(value, dict):
        for key in ("products", "items", "data"):
            if isinstance(value.get(key), list):
                value, container = value[key], key
                break
    if not isinstance(value, list) or not value or not all(isinstance(row, dict) for row in value[:5]):
        return None
    keys = set.intersection(*(set(row) for row in value[:5]))
    if (
        not keys.intersection({"id", "_id", "sku"})
        or not keys.intersection({"name", "title"})
        or not keys.intersection({"price", "prices", "variants"})
    ):
        return None
    target = public_url(endpoint)
    observed_fields = sorted(
        keys.intersection(
            {
                "id",
                "_id",
                "sku",
                "name",
                "title",
                "price",
                "prices",
                "variants",
                "stock",
                "in_stock",
                "is_in_stock",
                "description",
                "category",
                "images",
            }
        )
    )
    return {
        "id": hashlib.sha256(("catalog.read:" + target).encode()).hexdigest()[:20],
        "operation": "read_catalog_source",
        "status": "suggested",
        "execution_verified": False,
        "protocol_support": "not_tested",
        "source_url": public_url(source_url),
        "target_url": target,
        "evidence": (
            f"Page GET returned product-shaped JSON; container={container}; fields={','.join(observed_fields)}"
        ),
        "classification": "read_candidate",
        "confidence": "observed_response_shape",
        "requires_developer_wiring": True,
        "reason": (
            "Observed response only. Pagination, SKU, stock and authorization need review; "
            "no search or write parameters inferred."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


class PageEvidence(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.buttons = []
        self.forms = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            item = {"href": attrs["href"], "text": attrs.get("aria-label", "")[:200]}
            self.links.append(item)
            self.active = item
        elif tag == "button":
            item = {"text": attrs.get("aria-label", "")[:200]}
            self.buttons.append(item)
            self.active = item
        elif tag == "form":
            self.forms.append(
                {
                    "action": attrs.get("action", ""),
                    "method": attrs.get("method", "get").upper(),
                    "role": attrs.get("role", ""),
                }
            )

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"] = (self.active["text"] + " " + data.strip())[:200]

    def handle_endtag(self, tag):
        if tag in ("a", "button"):
            self.active = None


def extract_candidates(url, html):
    """No network, JS evaluation, endpoint guessing or inferred UCP conformance."""
    source = public_url(url)
    if len(html.encode("utf-8")) > 4_000_000:
        raise ValueError("Page evidence exceeds 4 MB")
    parser = PageEvidence()
    parser.feed(html)
    found = {}

    def add(operation, target, evidence, kind, reason):
        key = operation + ":" + (target or source)
        if key in found:
            return
        found[key] = {
            "id": hashlib.sha256(key.encode()).hexdigest()[:20],
            "operation": operation,
            "status": "suggested",
            "execution_verified": False,
            "protocol_support": "not_tested",
            "source_url": source,
            "target_url": target,
            "evidence": evidence[:200],
            "classification": kind,
            "confidence": "observed_ui_only",
            "requires_developer_wiring": True,
            "reason": reason,
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        }

    for link in parser.links[:2000]:
        try:
            raw = urljoin(source, link["href"])
            target = public_url(raw)
        except (ValueError, TypeError):
            continue
        if urlsplit(target).netloc != urlsplit(source).netloc:
            if urlsplit(target).hostname in ("wa.me", "api.whatsapp.com"):
                add(
                    "prepare_contact_handoff",
                    None,
                    "WhatsApp link observed",
                    "external_handoff",
                    "Preparing a link does not send a message; no recipient or message exported.",
                )
            continue
        # Query-bearing links can perform GET mutations. Never propose them as executable navigation.
        if urlsplit(raw).query:
            continue
        path = unquote(urlsplit(target).path).lower()
        if re.search(r"(?:^|/)(checkout|cart|basket)(?:/|$)", path):
            add(
                "navigate_checkout" if "checkout" in path else "navigate_cart",
                target,
                link["text"],
                "navigation_handoff",
                "Link observed only; does not create checkout or commit money. Do not execute without review.",
            )
        elif re.search(r"(?:^|/)(products?|productdetail|product-page)(?:/|$)", path):
            add(
                "view_product",
                target,
                link["text"],
                "read_navigation",
                "Product page link; API schema, SKU and stock are not verified.",
            )
        elif re.search(r"(?:^|/)(shop|collections?|product-category)(?:/|$)", path):
            add(
                "browse_catalog",
                target,
                link["text"],
                "read_navigation",
                "Collection link; not proof of a complete catalog API.",
            )
    for form in parser.forms[:100]:
        if form["role"] == "search":
            add(
                "search_products",
                None,
                "Search form observed",
                "read_candidate",
                "Search behavior, parameters and endpoint require developer inspection.",
            )
    for button in parser.buttons[:1000]:
        if re.search(r"add to (cart|bag)|הוספה לסל|הוסף לסל", button["text"], re.I):
            add(
                "add_to_cart",
                None,
                button["text"],
                "mutation_requires_review",
                "Button observed; auth, ownership, variant and mutation API are not verified. Not executed.",
            )
    return {
        "schema_version": 1,
        "source_url": source,
        "evidence_type": "page_html",
        "candidates": list(found.values())[:100],
        "candidate_limit": 100,
        "complete_catalog": False,
        "production_ready": False,
    }
