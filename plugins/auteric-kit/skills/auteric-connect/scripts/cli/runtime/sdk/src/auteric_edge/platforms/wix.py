"""Bounded Wix Cart V2 adapter; native catalog discovery is not connected.

Official Cart V2 schemas reviewed 2026-09-11. No Current Cart, Place Order,
payment, V1 fallback or automatic retry. Requires a site-scoped server API key.
"""

import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from ..connector import CommerceConnector
from ..http import request_json
from ..models import validate_output

STORES_APP = "215238eb-22a5-4c36-9e7b-e7c08025e04e"


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Wix requires a UUID identifier") from None


class WixConnector(CommerceConnector):
    supported_operations = frozenset(
        {
            "create_cart",
            "get_cart",
            "add_to_cart",
            "update_cart_item",
            "remove_from_cart",
            "create_checkout",
            "get_checkout",
        }
    )

    def __init__(self, *, site_id, api_key, currency, database, checkout_hosts, transport=None):
        self.site_id = identifier(site_id)
        if not isinstance(api_key, str) or not api_key or any(c.isspace() for c in api_key):
            raise ValueError("A local Wix API key is required")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("An explicit Wix base currency is required")
        if (
            not isinstance(checkout_hosts, (list, tuple))
            or not checkout_hosts
            or any(
                not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
                for host in checkout_hosts
            )
        ):
            raise ValueError("Exact checkout hostname allowlist is required")
        self.hosts, self.currency = frozenset(checkout_hosts), currency
        self.headers = {"Authorization": api_key, "wix-site-id": self.site_id}
        self.transport = transport
        self.path = Path(database)
        if str(database) == ":memory:" or self.path.is_symlink():
            raise ValueError("Private persistent local database required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS wix_carts (site TEXT, alias TEXT, remote TEXT, checkout TEXT, "
                "PRIMARY KEY(site,alias), UNIQUE(site,remote))"
            )
        self.path.chmod(0o600)

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def _remember(self, remote):
        remote = identifier(remote)
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO wix_carts VALUES (?,?,?,NULL)", (self.site_id, "wix_" + uuid4().hex, remote)
            )
            return db.execute(
                "SELECT alias FROM wix_carts WHERE site=? AND remote=?", (self.site_id, remote)
            ).fetchone()[0]

    def _resolve(self, alias):
        with self._db() as db:
            row = db.execute(
                "SELECT remote,checkout FROM wix_carts WHERE site=? AND alias=?", (self.site_id, alias)
            ).fetchone()
        if not row:
            raise ValueError("Unknown local Wix cart for this site")
        return row

    async def _call(self, path, body):
        # A new cookie jar for every call prevents cross-shopper session sharing.
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, follow_redirects=False, timeout=20
        ) as client:
            data = await request_json(
                client, "POST", "https://www.wixapis.com/ecom/v2/carts" + path, headers=self.headers, json=body
            )
        if not isinstance(data, dict):
            raise ValueError("Invalid Wix response envelope")
        return data

    @staticmethod
    def _input(item):
        # Catalog V3 references require an explicit variant; no default variant guessing.
        if not item.get("variant_id"):
            raise ValueError("Wix Catalog V3 requires an explicit variant_id")
        return {
            "catalogReference": {
                "appId": STORES_APP,
                "catalogItemId": identifier(item["product_id"]),
                "options": {"variantId": identifier(item["variant_id"])},
            },
            "quantity": item["quantity"],
        }

    def _normalize(self, payload, alias, remote):
        cart, summary = payload["cart"], payload["summary"]
        if cart["id"] != remote or summary["cartId"] != remote or summary["cartRevision"] != cart["revision"]:
            raise ValueError("Wix calculated cart identity or revision mismatch")
        if cart.get("orderPlaced") or cart.get("orderId") or cart.get("demo"):
            raise ValueError("Completed or demo Wix carts are unsupported")
        if (
            cart["businessInfo"]["currencyCode"] != self.currency
            or cart.get("customerInfo", {}).get("currencyCode", self.currency) != self.currency
        ):
            raise ValueError("Wix multi-currency cart is not connected")
        if summary.get("violations") or summary.get("calculationErrors") or summary.get("spiViolations"):
            raise ValueError("Wix calculation reported violations; no success inferred")
        lines = cart["lineItems"]
        prices = {row["lineItemId"]: row for row in summary["lineItems"]}
        if len(prices) != len(summary["lineItems"]) or set(prices) != {row["id"] for row in lines}:
            raise ValueError("Wix calculation is missing or duplicating line items")
        items = []
        for line in lines:
            ref = line["source"]["catalogReference"]
            quantity = line["quantityInfo"]
            if (
                line.get("customLineItem")
                or ref["appId"] != STORES_APP
                or line.get("modifierGroups")
                or line.get("status") != "IN_STOCK"
                or quantity["confirmedQuantity"] != quantity["requestedQuantity"]
            ):
                raise ValueError("Wix line is unsupported or quantity adjusted; reconcile before a write")
            price = prices[line["id"]]
            if price["quantity"] != quantity["confirmedQuantity"]:
                raise ValueError("Wix calculated quantity mismatch")
            items.append(
                {
                    "product_id": identifier(ref["catalogItemId"]),
                    "variant_id": identifier(ref["options"]["variantId"]),
                    "sku": line["attributes"]["physicalProperties"]["sku"],
                    "quantity": quantity["confirmedQuantity"],
                    "unit_price": price["unitPrice"]["amount"],
                    "total_price": price["totalPrice"]["amount"],
                }
            )
        total = summary["priceSummary"]
        # Canonical Cart has no shipping/fee fields. Fail closed instead of losing components.
        if Decimal(total["delivery"]["amount"]) != 0 or Decimal(total["additionalFees"]["amount"]) != 0:
            raise ValueError("Wix shipping or additional fees require extended canonical mapping")
        if sum((Decimal(item["total_price"]) for item in items), Decimal(0)) != Decimal(
            total["subtotal"]["amount"]
        ) or Decimal(total["subtotal"]["amount"]) - Decimal(total["discount"]["amount"]) + Decimal(
            total["tax"]["amount"]
        ) != Decimal(total["total"]["amount"]):
            raise ValueError("Wix calculated totals are inconsistent")
        return validate_output(
            "get_cart",
            {
                "id": alias,
                "items": items,
                "subtotal": total["subtotal"]["amount"],
                "discounts": total["discount"]["amount"],
                "tax": total["tax"]["amount"],
                "total": total["total"]["amount"],
                "currency": self.currency,
                "metadata": {"platform": "wix", "api": "cart-v2", "calculated": True},
            },
        )

    async def _snapshot(self, alias):
        remote, _ = self._resolve(alias)
        payload = await self._call(f"/{remote}/calculate", {"refreshCart": True})
        return self._normalize(payload, alias, remote), payload["cart"]

    async def search_products(self, request):
        raise NotImplementedError("Native Wix Catalog V3 search is not connected")

    async def get_product(self, request):
        raise NotImplementedError("Native Wix Catalog V3 authoritative variant pricing is not connected")

    async def create_cart(self, request):
        if request["currency"] != self.currency:
            raise ValueError("Requested currency differs from Wix base currency")
        lines = [self._input(item) for item in request["items"]]
        if len({(i["product_id"], i.get("variant_id")) for i in request["items"]}) != len(lines):
            raise ValueError("Duplicate Wix variant")
        payload = await self._call("", {"catalogItems": lines})
        alias = self._remember(payload["cart"]["id"])
        cart = await self.get_cart({"cart_id": alias})
        self._check_items(cart, request["items"])
        return cart

    @staticmethod
    def _check_items(cart, expected):
        def quantities(items):
            result = {}
            for item in items:
                key = (item["product_id"], item.get("variant_id"))
                result[key] = result.get(key, 0) + item["quantity"]
            return result

        if quantities(cart["items"]) != quantities(expected):
            raise ValueError("Wix mutation result differs from requested items; reconcile before another write")

    async def get_cart(self, request):
        return (await self._snapshot(request["cart_id"]))[0]

    async def _mutate(self, alias, suffix, body):
        remote, _ = self._resolve(alias)
        result = await self._call(f"/{remote}/{suffix}", body)
        if result["cart"]["id"] != remote:
            raise ValueError("Wix mutation returned a different cart; reconcile before another write")
        return await self.get_cart({"cart_id": alias})

    async def add_to_cart(self, request):
        line = self._input(request)
        before, _ = await self._snapshot(request["cart_id"])
        result = await self._mutate(request["cart_id"], "add-line-items", {"catalogItems": [line]})
        self._check_items(result, [*before["items"], request])
        return result

    async def _line_id(self, request):
        before, cart = await self._snapshot(request["cart_id"])
        product_id = identifier(request["product_id"])
        matches = [
            line for line in cart["lineItems"] if line["source"]["catalogReference"]["catalogItemId"] == product_id
        ]
        if len(matches) != 1:
            raise ValueError("Wix cart product is absent or variant-ambiguous; no write sent")
        return identifier(matches[0]["id"]), before

    async def update_cart_item(self, request):
        line, before = await self._line_id(request)
        result = await self._mutate(
            request["cart_id"],
            "update-line-items",
            {"lineItems": [{"lineItemId": line, "quantity": {"newQuantity": request["quantity"]}}]},
        )
        expected = [
            {**item, "quantity": request["quantity"]} if item["product_id"] == request["product_id"] else item
            for item in before["items"]
        ]
        self._check_items(result, expected)
        return result

    async def remove_from_cart(self, request):
        line, before = await self._line_id(request)
        result = await self._mutate(request["cart_id"], "remove-line-items", {"lineItemIds": [line]})
        self._check_items(result, [item for item in before["items"] if item["product_id"] != request["product_id"]])
        return result

    def _url(self, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Wix checkout URL is outside the configured HTTPS allowlist")
        return value

    async def create_checkout(self, request):
        cart = await self.get_cart(request)
        if not cart["items"] or Decimal(cart["total"]) <= 0:
            raise ValueError("Cannot hand off an empty or zero-total Wix cart")
        remote, _ = self._resolve(cart["id"])
        payload = await self._call(f"/{remote}/get-checkout-url", {})
        url = self._url(payload["checkoutUrl"])
        with self._db() as db:
            db.execute("UPDATE wix_carts SET checkout=? WHERE site=? AND alias=?", (url, self.site_id, cart["id"]))
        return self._checkout(cart, url)

    def _checkout(self, cart, url):
        return {
            "id": cart["id"],
            "cart_id": cart["id"],
            "status": "incomplete",
            "checkout_url": self._url(url),
            "total": cart["total"],
            "currency": self.currency,
            "metadata": {"payment_execution": False, "order_status_verified": False},
        }

    async def get_checkout(self, request):
        _, url = self._resolve(request["checkout_id"])
        if not url:
            raise ValueError("Wix checkout handoff has not been issued")
        cart = await self.get_cart({"cart_id": request["checkout_id"]})
        return self._checkout(cart, url)
