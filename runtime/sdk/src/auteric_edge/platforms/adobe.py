"""Adobe Commerce 2.4.8+ guest GraphQL adapter, simple products only.

No placeOrder/payment mutation. Hosted checkout requires a merchant bridge.
"""

import sqlite3
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

from ..connector import CommerceConnector
from ..http import request_json
from ..mapping import safe_base_url

PRODUCT = (
    "sku name __typename stock_status description { html } "
    "price_range { minimum_price { final_price { value currency } } }"
)
CART = """id itemsV2(pageSize:100) { total_count items { uid quantity product { sku }
prices { price { value currency } row_total { value currency } } } }
prices { subtotal_excluding_tax { value currency } grand_total { value currency }
applied_taxes { amount { value currency } } discounts { amount { value currency } } }"""


class AdobeCommerceConnector(CommerceConnector):
    def __init__(
        self, base_url, *, database, store_view="default", currency="USD", checkout_bridge=None, transport=None
    ):
        if not store_view or any(c in store_view for c in "\r\n"):
            raise ValueError("Invalid Adobe store view")
        self.url = safe_base_url(base_url) + "/graphql"
        self.scope = self.url + ":" + store_view
        self.currency, self.bridge = currency, checkout_bridge
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
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("Persistent cart alias database required")
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS adobe_carts(id TEXT PRIMARY KEY,scope TEXT,native TEXT)")
        Path(database).chmod(0o600)
        self.client = httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=20, follow_redirects=False, headers={"Store": store_view}
        )

    async def close(self):
        await self.client.aclose()

    async def gql(self, query, variables):
        result = await request_json(self.client, "POST", self.url, json={"query": query, "variables": variables})
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise RuntimeError("Adobe GraphQL error; inspect scopes/schema locally; no write retries")
        return result["data"]

    def native(self, alias):
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT native FROM adobe_carts WHERE id=? AND scope=?", (alias, self.scope)).fetchone()
        if not row:
            raise ValueError("Unknown Adobe cart alias")
        return row[0]

    def amount(self, value):
        if value["currency"] != self.currency:
            raise ValueError("Adobe currency mismatch")
        amount = Decimal(str(value["value"]))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal(".01")):
            raise ValueError("Unsupported monetary amount")
        return str(amount)

    def product(self, raw):
        if raw["__typename"] != "SimpleProduct":
            raise ValueError("Only simple products supported; configurable/bundle options require custom adapter")
        return {
            "id": raw["sku"],
            "sku": raw["sku"],
            "title": raw["name"],
            "description": raw.get("description", {}).get("html"),
            "price": self.amount(raw["price_range"]["minimum_price"]["final_price"]),
            "currency": self.currency,
            "availability": "in_stock" if raw["stock_status"] == "IN_STOCK" else "out_of_stock",
        }

    async def search_products(self, request):
        data = await self.gql(
            "query($search:String!,$limit:Int!){products(search:$search,pageSize:$limit){items{" + PRODUCT + "}}}",
            {"search": request["query"], "limit": request["limit"]},
        )
        return [self.product(p) for p in data["products"]["items"]]

    async def get_product(self, request):
        data = await self.gql(
            "query($sku:String!){products(filter:{sku:{eq:$sku}}){items{" + PRODUCT + "}}}",
            {"sku": request["product_id"]},
        )
        products = data["products"]["items"]
        if len(products) != 1 or products[0]["sku"] != request["product_id"]:
            raise ValueError("Product not found or ambiguous")
        return self.product(products[0])

    def cart(self, alias, raw):
        if raw["id"] != self.native(alias):
            raise ValueError("Adobe cart identity mismatch")
        rows = raw["itemsV2"]["items"]
        if raw["itemsV2"]["total_count"] != len(rows):
            raise ValueError("Cart truncated; more than 100 items unsupported")
        prices = raw["prices"]
        return {
            "id": alias,
            "currency": self.currency,
            "subtotal": self.amount(prices["subtotal_excluding_tax"]),
            "total": self.amount(prices["grand_total"]),
            "tax": str(sum((Decimal(self.amount(t["amount"])) for t in prices.get("applied_taxes") or []), Decimal(0))),
            "discounts": str(
                sum((Decimal(self.amount(t["amount"])) for t in prices.get("discounts") or []), Decimal(0))
            ),
            "items": [
                {
                    "product_id": i["product"]["sku"],
                    "sku": i["product"]["sku"],
                    "quantity": int(i["quantity"]) if i["quantity"] == int(i["quantity"]) else i["quantity"],
                    "unit_price": self.amount(i["prices"]["price"]),
                    "total_price": self.amount(i["prices"]["row_total"]),
                }
                for i in rows
            ],
        }

    async def raw_cart(self, alias):
        return (await self.gql("query($id:String!){cart(cart_id:$id){" + CART + "}}", {"id": self.native(alias)}))[
            "cart"
        ]

    async def create_cart(self, request):
        if request.get("items") or request["currency"] != self.currency:
            raise ValueError("Create empty cart in configured currency; add items separately")
        raw = (await self.gql("mutation{createGuestCart{cart{id}}}", {}))["createGuestCart"]["cart"]
        alias = uuid4().hex
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO adobe_carts VALUES(?,?,?)", (alias, self.scope, raw["id"]))
        return await self.get_cart({"cart_id": alias})

    async def get_cart(self, request):
        return self.cart(request["cart_id"], await self.raw_cart(request["cart_id"]))

    async def add_to_cart(self, request):
        if request.get("variant_id"):
            raise ValueError("Configurable variants require custom adapter")
        await self.get_product({"product_id": request["product_id"]})
        data = await self.gql(
            "mutation($id:String!,$items:[CartItemInput!]!){"
            "addProductsToCart(cartId:$id,cartItems:$items){user_errors{code} cart{" + CART + "}}}",
            {
                "id": self.native(request["cart_id"]),
                "items": [{"sku": request["product_id"], "quantity": request["quantity"]}],
            },
        )
        result = data["addProductsToCart"]
        if result.get("user_errors"):
            raise RuntimeError("Adobe add-item reported errors; reconcile possible partial write")
        return self.cart(request["cart_id"], result["cart"])

    async def change(self, request, quantity):
        raw = await self.raw_cart(request["cart_id"])
        self.cart(request["cart_id"], raw)
        matches = [i for i in raw["itemsV2"]["items"] if i["product"]["sku"] == request["product_id"]]
        if len(matches) != 1:
            raise ValueError("Cart item absent or ambiguous")
        result = await self.gql(
            "mutation($input:UpdateCartItemsInput!){updateCartItems(input:$input){cart{" + CART + "}}}",
            {
                "input": {
                    "cart_id": self.native(request["cart_id"]),
                    "cart_items": [{"cart_item_uid": matches[0]["uid"], "quantity": quantity}],
                }
            },
        )
        return self.cart(request["cart_id"], result["updateCartItems"]["cart"])

    async def update_cart_item(self, request):
        return await self.change(request, request["quantity"])

    async def remove_from_cart(self, request):
        return await self.change(request, 0)

    async def create_checkout(self, request):
        if not self.bridge:
            raise ValueError("Merchant checkout session bridge required; no generic Adobe hosted URL")
        return await self.bridge.create_checkout(await self.get_cart(request), self.native(request["cart_id"]))

    async def get_checkout(self, request):
        if not self.bridge:
            raise ValueError("Merchant checkout session bridge required")
        return await self.bridge.get_checkout(request)
