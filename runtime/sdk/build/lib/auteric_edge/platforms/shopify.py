"""Shopify Storefront adapter, pinned to 2026-07.

Sellable canonical product IDs are ProductVariant GIDs, never parent Product IDs.
Checkout is a hosted handoff, not payment execution or order-status verification.
Requires Storefront product and checkout permissions; no Admin token is needed.
Cart replacement/cancellation are deliberately unsupported: no atomic API exists.
"""

import re
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from ..connector import CommerceConnector
from ..http import request_json
from .shopify_store import ShopifyStore

VARIANT = """id sku title availableForSale price { amount currencyCode }
image { url } product { id title description onlineStoreUrl }"""
CART = """id checkoutUrl cost { subtotalAmount { amount currencyCode }
totalAmount { amount currencyCode } totalTaxAmount { amount currencyCode } }
lines(first: 250) { pageInfo { hasNextPage } nodes { id quantity
merchandise { ... on ProductVariant { id sku } }
cost { amountPerQuantity { amount currencyCode } totalAmount { amount currencyCode } } } }"""


class ShopifyConnector(CommerceConnector):
    def __init__(
        self,
        shop,
        token,
        *,
        database,
        state=None,
        currency="USD",
        api_version="2026-07",
        transport=None,
        token_type="public",
        checkout_hosts=None,
    ):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
            raise ValueError("Use the store's exact lowercase myshopify.com hostname")
        if api_version != "2026-07":
            raise ValueError("Only the reviewed Shopify API version 2026-07 is supported")
        if not token or any(c.isspace() for c in token):
            raise ValueError("A local Storefront access token is required")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Invalid store currency")
        if token_type not in ("public", "private"):
            raise ValueError("Shopify token_type must be public or private")
        if checkout_hosts is not None and not isinstance(checkout_hosts, (list, tuple)):
            raise ValueError("checkout_hosts must be a list of exact hostnames")
        self.checkout_hosts = frozenset(checkout_hosts if checkout_hosts is not None else [shop])
        if not self.checkout_hosts or any(
            not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
            for host in self.checkout_hosts
        ):
            raise ValueError("Invalid checkout hostname allowlist")
        self.currency = currency
        self.url = f"https://{shop}/api/{api_version}/graphql.json"
        # Hosted runtimes can inject a distributed alias store. The SQLite
        # implementation remains the safe reference/default for edge installs.
        self.state = state or ShopifyStore(database, shop)
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            headers={
                "X-Shopify-Storefront-Access-Token"
                if token_type == "public"
                else "Shopify-Storefront-Private-Token": token
            },
        )

    async def close(self):
        await self.client.aclose()

    async def _graphql(self, query, variables):
        # No automatic retries, including throttled/ambiguous write responses.
        try:
            payload = await request_json(self.client, "POST", self.url, json={"query": query, "variables": variables})
        except httpx.HTTPError:
            raise RuntimeError("Shopify transport failed; writes must be reconciled, not retried") from None
        if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise RuntimeError("Shopify GraphQL request failed; verify token scopes and API version locally")
        return payload["data"]

    @staticmethod
    def _variant_id(value):
        if not re.fullmatch(r"gid://shopify/ProductVariant/[0-9]+", value):
            raise ValueError("product_id must be a sellable Shopify ProductVariant GID")
        return value

    def _money(self, value):
        if value["currencyCode"] != self.currency:
            raise ValueError(
                "Shopify currency differs from configured currency; multi-market checkout is not connected"
            )
        return value["amount"]

    def _product(self, value):
        if not value or not value.get("sku"):
            raise ValueError("Shopify variant is unavailable or missing its required SKU")
        product = value["product"]
        return {
            "id": self._variant_id(value["id"]),
            "sku": value["sku"],
            "title": product["title"] + (" / " + value["title"] if value["title"] != "Default Title" else ""),
            "description": product.get("description"),
            "price": self._money(value["price"]),
            "currency": self.currency,
            "availability": "in_stock" if value["availableForSale"] else "out_of_stock",
            "images": [value["image"]["url"]] if value.get("image") else [],
            "product_url": product.get("onlineStoreUrl"),
            "metadata": {"platform": "shopify", "parent_product_id": product["id"], "sellable_unit": "variant"},
        }

    async def search_products(self, request):
        # Bounded product search with explicit variant pagination, never silently
        # pretend the first variant represents all prices for a product.
        query = (
            "query($query:String!,$limit:Int!) { products(first:$limit,query:$query) "
            "{ nodes { id variants(first:100) { nodes { " + VARIANT + " } pageInfo { hasNextPage endCursor } } } } }"
        )
        data = await self._graphql(query, {"query": request["query"], "limit": request["limit"]})
        products = []
        for parent in data["products"]["nodes"]:
            connection = parent["variants"]
            while True:
                for variant in connection["nodes"]:
                    # A merchant catalog can contain draft/incomplete variants.
                    # They are not valid canonical sellable units, but one bad
                    # record must not make the entire discovery result fail.
                    if not variant.get("sku"):
                        continue
                    products.append(self._product(variant))
                    if len(products) == request["limit"]:
                        return products
                if not connection["pageInfo"]["hasNextPage"]:
                    break
                data = await self._graphql(
                    "query($id:ID!,$after:String!) { product(id:$id) { variants(first:100,after:$after) { nodes { "
                    + VARIANT
                    + " } pageInfo { hasNextPage endCursor } } } }",
                    {"id": parent["id"], "after": connection["pageInfo"]["endCursor"]},
                )
                connection = data["product"]["variants"]
        return products

    async def get_product(self, request):
        result = await self._graphql(
            "query($id:ID!) { node(id:$id) { ... on ProductVariant { " + VARIANT + " } } }",
            {"id": self._variant_id(request["product_id"])},
        )
        product = self._product(result["node"])
        if product["id"] != request["product_id"]:
            raise ValueError("Shopify variant identity mismatch")
        return product

    @staticmethod
    def _lines(cart):
        if cart["lines"]["pageInfo"]["hasNextPage"]:
            raise ValueError("Shopify cart exceeds the supported 250-line bound")
        return cart["lines"]["nodes"]

    def _cart(self, cart):
        if not cart:
            raise ValueError("Shopify cart expired or is unavailable")
        items = []
        for line in self._lines(cart):
            merch, cost = line["merchandise"], line["cost"]
            if not merch.get("sku"):
                raise ValueError("Shopify cart contains a variant without SKU")
            items.append(
                {
                    "product_id": self._variant_id(merch["id"]),
                    "sku": merch["sku"],
                    "quantity": line["quantity"],
                    "unit_price": self._money(cost["amountPerQuantity"]),
                    "total_price": self._money(cost["totalAmount"]),
                }
            )
        cost = cart["cost"]
        return {
            "id": self.state.remember(cart["id"]),
            "items": items,
            "subtotal": self._money(cost["subtotalAmount"]),
            "total": self._money(cost["totalAmount"]),
            "tax": self._money(cost["totalTaxAmount"]) if cost.get("totalTaxAmount") else "0",
            "currency": self.currency,
            "metadata": {"platform": "shopify", "totals_estimated": True},
        }

    async def _raw_cart(self, alias):
        remote = self.state.resolve(alias)
        data = await self._graphql("query($id:ID!) { cart(id:$id) { " + CART + " } }", {"id": remote})
        cart = data["cart"]
        if not cart or cart["id"] != remote:
            raise ValueError("Shopify cart expired or identity mismatch")
        self._lines(cart)
        return cart

    def _input_line(self, request):
        variant = self._variant_id(request["product_id"])
        if request.get("variant_id") not in (None, variant):
            raise ValueError("variant_id must match the canonical sellable product_id")
        return {"merchandiseId": variant, "quantity": request["quantity"]}

    async def _mutation(self, name, declarations, arguments, variables):
        result = await self._graphql(
            f"mutation({declarations}) {{ {name}({arguments}) {{ cart {{ {CART} }} "
            "userErrors { code } warnings { code } } }",
            variables,
        )
        payload = result[name]
        if payload.get("userErrors") or payload.get("warnings") or not payload.get("cart"):
            raise RuntimeError("Shopify cart mutation was rejected or adjusted; reconcile before another write")
        return payload["cart"]

    async def create_cart(self, request):
        if request["currency"] != self.currency:
            raise ValueError("Requested currency differs from Shopify store currency")
        lines = [self._input_line(item) for item in request["items"]]
        if len({line["merchandiseId"] for line in lines}) != len(lines):
            raise ValueError("Duplicate Shopify variant")
        cart = await self._mutation("cartCreate", "$input:CartInput!", "input:$input", {"input": {"lines": lines}})
        return self._cart(cart)

    async def get_cart(self, request):
        return self._cart(await self._raw_cart(request["cart_id"]))

    async def add_to_cart(self, request):
        line = self._input_line(request)
        remote = self.state.resolve(request["cart_id"])
        cart = await self._mutation(
            "cartLinesAdd",
            "$cartId:ID!,$lines:[CartLineInput!]!",
            "cartId:$cartId,lines:$lines",
            {"cartId": remote, "lines": [line]},
        )
        if cart["id"] != remote:
            raise ValueError("Shopify cart identity mismatch")
        return self._cart(cart)

    async def _change(self, request, remove):
        raw = await self._raw_cart(request["cart_id"])
        matches = [
            line for line in self._lines(raw) if line["merchandise"]["id"] == self._variant_id(request["product_id"])
        ]
        if len(matches) != 1:
            raise ValueError("Shopify cart line is absent or ambiguous; no mutation sent")
        if remove:
            name, declarations, arguments = (
                "cartLinesRemove",
                "$cartId:ID!,$lineIds:[ID!]!",
                "cartId:$cartId,lineIds:$lineIds",
            )
            variables = {"cartId": raw["id"], "lineIds": [matches[0]["id"]]}
        else:
            name, declarations, arguments = (
                "cartLinesUpdate",
                "$cartId:ID!,$lines:[CartLineUpdateInput!]!",
                "cartId:$cartId,lines:$lines",
            )
            variables = {"cartId": raw["id"], "lines": [{"id": matches[0]["id"], "quantity": request["quantity"]}]}
        updated = await self._mutation(name, declarations, arguments, variables)
        if updated["id"] != raw["id"]:
            raise ValueError("Shopify cart identity mismatch")
        return self._cart(updated)

    async def update_cart_item(self, request):
        return await self._change(request, False)

    async def remove_from_cart(self, request):
        return await self._change(request, True)

    async def create_checkout(self, request):
        raw = await self._raw_cart(request["cart_id"])
        cart = self._cart(raw)
        if not cart["items"] or Decimal(cart["total"]) <= 0:
            raise ValueError("Cannot hand off an empty or zero-total cart")
        checkout_url = raw.get("checkoutUrl", "")
        parsed = urlsplit(checkout_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.checkout_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Shopify checkout URL is outside the configured HTTPS allowlist")
        return {
            "id": "checkout_" + cart["id"],
            "cart_id": cart["id"],
            "status": "incomplete",
            "total": cart["total"],
            "currency": cart["currency"],
            "checkout_url": checkout_url,
            "metadata": {
                "platform": "shopify",
                "hosted_handoff": True,
                "payment_execution": False,
                "order_status_verified": False,
                "totals_estimated": True,
            },
        }

    async def get_checkout(self, request):
        if not request["checkout_id"].startswith("checkout_cart_"):
            raise ValueError("Unknown Shopify checkout")
        return await self.create_checkout({"cart_id": request["checkout_id"][len("checkout_") :]})
