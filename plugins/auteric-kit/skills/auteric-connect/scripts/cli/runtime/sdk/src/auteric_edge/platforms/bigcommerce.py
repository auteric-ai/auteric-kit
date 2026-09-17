"""BigCommerce REST Management adapter for guest, base-currency carts.

Requires Products read-only and Carts modify scopes. Catalog prices are base
catalog prices, not shopper price-list quotes. No orders or payments are made.
Checkout links are provider-issued, single-use handoff links. Atomic replacement
is intentionally unsupported. Validate against the merchant's staging channel.
"""

import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from ..connector import CommerceConnector
from ..http import request_json
from ..models import Cart, Product


def _number(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("BigCommerce requires positive numeric product/variant IDs")
    return int(value)


def _cart_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid BigCommerce cart ID") from None


def _money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Unsupported or invalid BigCommerce monetary amount")
    return str(amount)


class BigCommerceConnector(CommerceConnector):
    """One connector instance is permanently scoped to a store and channel."""

    def __init__(self, *, store_hash, access_token, currency, channel_id=1, state_path, checkout_hosts, transport=None):
        if not re.fullmatch(r"[a-z0-9]{2,64}", store_hash):
            raise ValueError("Invalid BigCommerce store hash")
        if not access_token or "\n" in access_token or "\r" in access_token:
            raise ValueError("A local BigCommerce token is required")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Explicit store base currency is required")
        self.channel = _number(channel_id)
        self.currency = currency
        self.base = f"https://api.bigcommerce.com/stores/{store_hash}/v3"
        self.headers = {"X-Auth-Token": access_token, "Accept": "application/json"}
        self.transport = transport
        if not isinstance(checkout_hosts, (list, tuple)):
            raise ValueError("Explicit checkout hostname allowlist must be a list")
        self.hosts = frozenset(checkout_hosts)
        if not self.hosts or any(not re.fullmatch(r"[a-z0-9.-]+", host) for host in self.hosts):
            raise ValueError("Explicit checkout hostname allowlist is required")
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS bc_checkouts (scope TEXT, id TEXT, url TEXT, PRIMARY KEY(scope,id))")
        self.path.chmod(0o600)
        self.scope = f"{store_hash}:{self.channel}"

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    async def _request(self, method, path, **kwargs):
        # Each request has one attempt. Redirects and ambient proxy credentials are disabled.
        async with httpx.AsyncClient(
            timeout=20, trust_env=False, follow_redirects=False, transport=self.transport
        ) as client:
            result = await request_json(client, method, self.base + path, headers=self.headers, **kwargs)
        if not isinstance(result, dict) or "data" not in result:
            raise ValueError("Invalid BigCommerce response envelope")
        return result["data"]

    async def _assigned(self, product_id):
        rows = await self._request(
            "GET",
            "/catalog/products/channel-assignments",
            params={"product_id:in": str(product_id), "channel_id:in": str(self.channel), "limit": 250},
        )
        return any(row.get("product_id") == product_id and row.get("channel_id") == self.channel for row in rows)

    def _product(self, row):
        if not row.get("is_visible") or row.get("availability") == "disabled":
            raise ValueError("Product is not offered by the configured channel")
        availability = "preorder" if row.get("availability") == "preorder" else "unknown"
        inventory = None
        if row.get("inventory_tracking") == "product":
            inventory = row["inventory_level"]
            availability = "in_stock" if inventory > 0 else "out_of_stock"
        price = row.get("sale_price") or row["price"]
        return {
            "id": str(row["id"]),
            "sku": row["sku"],
            "title": row["name"],
            "description": row.get("description"),
            "price": _money(price),
            "currency": self.currency,
            "availability": availability,
            "inventory": inventory,
            "variants": [{"id": str(v["id"]), "sku": v.get("sku", "")} for v in row.get("variants", [])],
            "images": [i["url_standard"] for i in row.get("images", []) if i.get("url_standard")],
            "metadata": {"price_basis": "base_catalog_not_shopper_quote", "channel_id": self.channel},
        }

    async def search_products(self, request):
        rows = await self._request(
            "GET",
            "/catalog/products",
            params={
                "keyword": request["query"],
                "limit": request["limit"],
                "is_visible": "true",
                "include": "variants,images",
            },
        )
        result = []
        for row in rows:
            if row.get("availability") != "disabled" and await self._assigned(row["id"]):
                result.append(self._product(row))
        return result

    async def get_product(self, request):
        product_id = _number(request["product_id"])
        if not await self._assigned(product_id):
            raise ValueError("Product is not assigned to this channel")
        row = await self._request("GET", f"/catalog/products/{product_id}", params={"include": "variants,images"})
        if row.get("id") != product_id:
            raise ValueError("Mismatched product identity")
        return self._product(row)

    async def _line(self, item):
        product = Product.model_validate(await self.get_product({"product_id": item["product_id"]})).model_dump()
        result = {"product_id": _number(item["product_id"]), "quantity": item["quantity"]}
        if item.get("variant_id") is not None:
            variant = _number(item["variant_id"])
            if str(variant) not in {v["id"] for v in product["variants"]}:
                raise ValueError("Variant does not belong to product")
            result["variant_id"] = variant
        return result

    def _cart(self, row, expected=None):
        cart_id = _cart_id(row["id"])
        if expected is not None and cart_id != expected:
            raise ValueError("Mismatched cart identity")
        if row.get("channel_id") != self.channel or row.get("customer_id", 0) != 0:
            raise ValueError("Cart is outside the configured guest channel")
        if row["currency"]["code"] != self.currency:
            raise ValueError("Cart currency differs from configured base currency")
        lines = row["line_items"]
        if lines.get("gift_certificates") or lines.get("custom_items"):
            raise ValueError("Gift certificates and custom items are not supported")
        items = [
            {
                "product_id": str(i["product_id"]),
                "variant_id": str(i["variant_id"]) if i.get("variant_id") else None,
                "sku": i["sku"],
                "quantity": i["quantity"],
                "unit_price": _money(i["sale_price"]),
                "total_price": _money(i["extended_sale_price"]),
            }
            for key in ("physical_items", "digital_items")
            for i in lines.get(key, [])
        ]
        return {
            "id": cart_id,
            "items": items,
            "subtotal": _money(row["base_amount"]),
            "discounts": _money(row["discount_amount"]),
            "total": _money(row["cart_amount"]),
            "currency": self.currency,
            "metadata": {"totals_scope": "cart_before_checkout_shipping_and_tax"},
        }

    async def _raw_cart(self, cart_id):
        cart_id = _cart_id(cart_id)
        row = await self._request("GET", f"/carts/{cart_id}")
        # Internal calls do not pass through CommerceConnector.execute. Validate
        # the complete authoritative snapshot before allowing a subsequent write.
        Cart.model_validate(self._cart(row, cart_id))
        return row

    async def create_cart(self, request):
        if request["currency"] != self.currency:
            raise ValueError("Only configured base currency is supported")
        if not request["items"]:
            raise ValueError("BigCommerce requires an initial cart item")
        lines = [await self._line(item) for item in request["items"]]
        row = await self._request(
            "POST",
            "/carts",
            json={"channel_id": self.channel, "currency": {"code": self.currency}, "line_items": lines},
        )
        return self._cart(row)

    async def get_cart(self, request):
        return self._cart(await self._raw_cart(request["cart_id"]))

    async def add_to_cart(self, request):
        cart_id = _cart_id(request["cart_id"])
        await self._raw_cart(cart_id)
        line = await self._line(request)
        return self._cart(await self._request("POST", f"/carts/{cart_id}/items", json={"line_items": [line]}), cart_id)

    async def _item(self, request):
        row = await self._raw_cart(request["cart_id"])
        items = [i for key in ("physical_items", "digital_items") for i in row["line_items"].get(key, [])]
        matching = [i for i in items if str(i["product_id"]) == request["product_id"]]
        if len(matching) != 1:
            raise ValueError("A product must identify exactly one cart line; variant ambiguity is unsupported")
        return _cart_id(row["id"]), _cart_id(matching[0]["id"]), items, matching[0]

    async def update_cart_item(self, request):
        cart_id, item_id, _, item = await self._item(request)
        payload = {"quantity": request["quantity"], "product_id": item["product_id"]}
        if item.get("variant_id"):
            payload["variant_id"] = item["variant_id"]
        return self._cart(
            await self._request("PUT", f"/carts/{cart_id}/items/{item_id}", json={"line_item": payload}), cart_id
        )

    async def remove_from_cart(self, request):
        cart_id, item_id, items, _ = await self._item(request)
        if len(items) == 1:
            raise ValueError("Removing the final line deletes the BigCommerce cart; explicit cancellation is required")
        return self._cart(await self._request("DELETE", f"/carts/{cart_id}/items/{item_id}"), cart_id)

    def _checkout_url(self, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname not in self.hosts
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError("Checkout URL is outside the configured HTTPS allowlist")
        return value

    async def create_checkout(self, request):
        cart = await self.get_cart(request)
        if not cart["items"]:
            raise ValueError("Cannot check out an empty cart")
        response = await self._request("POST", f"/carts/{cart['id']}/redirect_urls", json={})
        url = self._checkout_url(response["checkout_url"])
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO bc_checkouts VALUES (?,?,?)", (self.scope, cart["id"], url))
        return await self.get_checkout({"checkout_id": cart["id"]})

    async def get_checkout(self, request):
        checkout_id = _cart_id(request["checkout_id"])
        with self._db() as db:
            row = db.execute(
                "SELECT url FROM bc_checkouts WHERE scope=? AND id=?", (self.scope, checkout_id)
            ).fetchone()
        if row is None:
            raise ValueError("Checkout was not issued by this connector")
        result = await self._request("GET", f"/checkouts/{checkout_id}")
        if result.get("id") != checkout_id:
            raise ValueError("Mismatched checkout identity")
        cart = self._cart(result["cart"], checkout_id)
        return {
            "id": checkout_id,
            "cart_id": checkout_id,
            "status": "incomplete",
            "checkout_url": self._checkout_url(row[0]),
            "total": _money(result["grand_total"]),
            "currency": cart["currency"],
            "metadata": {"payment_execution": False, "single_use_handoff": True, "completion_status": "not_observed"},
        }
