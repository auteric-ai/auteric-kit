"""WooCommerce Store API v1. Checkout needs an explicit merchant session bridge.

https://developer.woocommerce.com/docs/apis/store-api/resources-endpoints/cart/
Never calls native POST /checkout (which can place an order/process payment).
"""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

from ..connector import CommerceConnector
from ..mapping import safe_base_url
from ..models import validate_output


def money(value, unit):
    if unit != 2:
        raise ValueError("This connector currently requires two-decimal currency")
    return str(Decimal(str(value)) / 100)


class WooCommerceConnector(CommerceConnector):
    def __init__(self, base_url, *, database, checkout_bridge=None, transport=None):
        self.base = safe_base_url(base_url) + "/wp-json/wc/store/v1"
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("Cart tokens require persistent private storage")
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS woo_carts(id TEXT PRIMARY KEY, origin TEXT, token TEXT)")
        self.bridge = checkout_bridge
        self.supported_operations = {
            "search_products",
            "get_product",
            "create_cart",
            "get_cart",
            "add_to_cart",
            "update_cart_item",
            "remove_from_cart",
        }
        if checkout_bridge:
            self.supported_operations.update({"create_checkout", "get_checkout"})
        Path(database).chmod(0o600)
        self.transport = transport

    def token(self, cart_id):
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT token FROM woo_carts WHERE id=? AND origin=?", (cart_id, self.base)).fetchone()
        if not row:
            raise ValueError("Unknown local WooCommerce cart")
        return row[0]

    async def call(self, method, path, *, cart_id=None, body=None, params=None):
        # Separate cookie jars are deliberate: never share a Woo buyer session.
        headers = {"Cart-Token": self.token(cart_id)} if cart_id else {}
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, follow_redirects=False, timeout=15
        ) as client:
            async with client.stream(method, self.base + path, headers=headers, json=body, params=params) as response:
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(f"WooCommerce HTTP {response.status_code}; no write retry")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > 2_000_000:
                        raise ValueError("WooCommerce response exceeds limit")
                    data.extend(chunk)
                value = json.loads(data)
                token = response.headers.get("Cart-Token")
        if token and cart_id:
            with sqlite3.connect(self.database) as db:
                db.execute("UPDATE woo_carts SET token=? WHERE id=? AND origin=?", (token, cart_id, self.base))
        return value, token

    @staticmethod
    def product(raw):
        if raw.get("type") == "variable" or raw.get("prices", {}).get("price_range"):
            raise ValueError("Variable products require an explicitly selected variation adapter")
        prices = raw["prices"]
        return validate_output(
            "get_product",
            {
                "id": str(raw["id"]),
                "sku": raw["sku"],
                "title": raw["name"],
                "description": raw.get("description"),
                "price": money(prices["price"], prices["currency_minor_unit"]),
                "currency": prices["currency_code"],
                "availability": "in_stock" if raw["is_in_stock"] else "out_of_stock",
                "images": [i["src"] for i in raw.get("images", [])],
                "product_url": raw.get("permalink"),
                "metadata": {"categories": [c["name"] for c in raw.get("categories", [])]},
            },
        )

    async def search_products(self, request):
        raw, _ = await self.call(
            "GET", "/products", params={"search": request["query"], "per_page": request["limit"], "type": "simple"}
        )
        return [self.product(p) for p in raw]

    async def get_product(self, request):
        identifier = request["product_id"]
        if not identifier.isdigit():
            raise ValueError("WooCommerce product ID must be numeric")
        raw, _ = await self.call("GET", "/products/" + identifier)
        if str(raw["id"]) != identifier:
            raise ValueError("Wrong product identity")
        return self.product(raw)

    def cart(self, cart_id, raw):
        totals = raw["totals"]
        unit = totals["currency_minor_unit"]
        if Decimal(totals.get("total_shipping") or "0") or Decimal(totals.get("total_fees") or "0"):
            raise ValueError("Shipping/fee carts require an extended canonical totals adapter")
        return validate_output(
            "get_cart",
            {
                "id": cart_id,
                "currency": totals["currency_code"],
                "subtotal": money(totals["total_items"], unit),
                "discounts": money(totals["total_discount"], unit),
                "tax": money(totals["total_tax"], unit),
                "total": money(totals["total_price"], unit),
                "items": [
                    {
                        "product_id": str(i["id"]),
                        "sku": i["sku"],
                        "quantity": i["quantity"],
                        "unit_price": money(i["prices"]["price"], unit),
                        "total_price": money(i["totals"]["line_total"], unit),
                    }
                    for i in raw["items"]
                ],
            },
        )

    async def create_cart(self, request):
        if request.get("items"):
            raise ValueError("Create an empty cart then add items; no non-atomic create/add emulation")
        raw, token = await self.call("GET", "/cart")
        if not token or raw["items"]:
            raise ValueError("Expected a new isolated empty cart and Cart-Token")
        identifier = uuid4().hex
        result = self.cart(identifier, raw)
        if result["currency"] != request["currency"]:
            raise ValueError("Store currency differs from requested cart currency")
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO woo_carts VALUES(?,?,?)", (identifier, self.base, token))
        return result

    async def get_cart(self, request):
        raw, _ = await self.call("GET", "/cart", cart_id=request["cart_id"])
        return self.cart(request["cart_id"], raw)

    async def add_to_cart(self, request):
        if request.get("variant_id"):
            raise ValueError("Variation selection requires a configured variation adapter")
        if not request["product_id"].isdigit():
            raise ValueError("WooCommerce product ID must be numeric")
        raw, _ = await self.call(
            "POST",
            "/cart/add-item",
            cart_id=request["cart_id"],
            body={"id": int(request["product_id"]), "quantity": request["quantity"]},
        )
        return self.cart(request["cart_id"], raw)

    async def change_item(self, request, remove):
        raw, _ = await self.call("GET", "/cart", cart_id=request["cart_id"])
        matches = [i for i in raw["items"] if str(i["id"]) == request["product_id"]]
        if len(matches) != 1:
            raise ValueError("Cart item absent or ambiguous")
        body = {"key": matches[0]["key"]}
        if not remove:
            body["quantity"] = request["quantity"]
        raw, _ = await self.call(
            "POST", "/cart/" + ("remove-item" if remove else "update-item"), cart_id=request["cart_id"], body=body
        )
        return self.cart(request["cart_id"], raw)

    async def update_cart_item(self, request):
        return await self.change_item(request, False)

    async def remove_from_cart(self, request):
        return await self.change_item(request, True)

    async def create_checkout(self, request):
        if self.bridge is None:
            raise ValueError("Configure merchant checkout session bridge; native checkout POST can place orders/pay")
        cart = await self.get_cart(request)
        result = await self.bridge.create_checkout(cart, self.token(request["cart_id"]))
        if result["cart_id"] != cart["id"] or result["total"] != cart["total"]:
            raise ValueError("Checkout bridge cart/total mismatch")
        return validate_output("create_checkout", result)

    async def get_checkout(self, request):
        if self.bridge is None:
            raise ValueError("Merchant checkout bridge not configured")
        return validate_output("get_checkout", await self.bridge.get_checkout(request))
