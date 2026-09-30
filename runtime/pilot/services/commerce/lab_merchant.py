"""Deterministic, nonfinancial merchant for local integration exercises only."""

from copy import deepcopy
from decimal import Decimal

from auteric_edge.connector import MockConnector


class LabMerchant(MockConnector):
    """Real in-memory mutations, never a connection to a live merchant."""

    WRITES = frozenset({"create_cart", "add_to_cart", "update_cart_item", "remove_from_cart",
                        "replace_cart_items", "cancel_cart", "create_checkout"})

    def __init__(self):
        super().__init__()
        self.products = {
            "demo-shoes": self._product("demo-shoes", "LAB-SHOES", "Local Demo Shoes", "100.00", 12),
            "demo-ring": self._product("demo-ring", "LAB-RING", "Local Demo Ring", "60.00", 8),
            "demo-bag": self._product("demo-bag", "LAB-BAG", "Local Demo Bag", "250.00", 3),
        }
        self.products["demo-ring"]["variants"] = [
            {"id": "ring-small", "sku": "LAB-RING-S", "price": "60.00", "inventory": 5,
             "availability": "in_stock", "currency": "USD"},
            {"id": "ring-large", "sku": "LAB-RING-L", "price": "80.00", "inventory": 3,
             "availability": "in_stock", "currency": "USD"},
        ]
        self.fail_next_write = False
        self.execution_counts = {}

    @staticmethod
    def _product(identifier, sku, title, price, stock):
        return {"id": identifier, "sku": sku, "title": title, "price": price,
                "currency": "USD", "inventory": stock, "availability": "in_stock",
                "metadata": {"local_lab": True}}

    async def execute(self, operation, request, mapping=None):
        result = await super().execute(operation, request, mapping)
        self.execution_counts[operation] = self.execution_counts.get(operation, 0) + 1
        if operation in self.WRITES and self.fail_next_write:
            self.fail_next_write = False
            raise TimeoutError("Local lab: response lost after merchant mutation")
        return deepcopy(result)

    async def search_products(self, request):
        return deepcopy(await super().search_products(request))

    async def get_product(self, request):
        for product in self.products.values():
            for variant in product.get("variants", []):
                if variant["id"] == request["product_id"]:
                    return {**deepcopy(product), **deepcopy(variant), "variants": [],
                            "metadata": {"local_lab": True, "parent_product_id": product["id"]}}
        return deepcopy(await super().get_product(request))

    def _items(self, items):
        result, seen = [], set()
        for item in items:
            product = self.products[item["product_id"]]
            # Canonical update/remove identify a product, not a variant. Reject
            # multiple variants of one product rather than mutate an ambiguous line.
            if product["id"] in seen:
                raise ValueError("One line per product is supported; replace the selected variant")
            seen.add(product["id"])
            variant_id = item.get("variant_id")
            source = product
            if variant_id is not None:
                source = next((v for v in product.get("variants", []) if v["id"] == variant_id), None)
                if source is None:
                    raise ValueError("Unknown product variant")
            elif product.get("variants"):
                raise ValueError("Select a product variant")
            quantity = item["quantity"]
            if quantity > source["inventory"] or source["availability"] != "in_stock":
                raise ValueError("Insufficient inventory")
            result.append({"product_id": product["id"], "variant_id": variant_id,
                           "sku": source["sku"], "quantity": quantity,
                           "unit_price": source["price"],
                           "total_price": str(Decimal(source["price"]) * quantity)})
        return result

    @staticmethod
    def _inputs(cart):
        return [{"product_id": i["product_id"], "variant_id": i.get("variant_id"),
                 "quantity": i["quantity"]} for i in cart["items"]]

    async def add_to_cart(self, request):
        cart = self._active(request["cart_id"])
        items = self._inputs(cart)
        existing = next((i for i in items if i["product_id"] == request["product_id"]), None)
        if existing:
            if existing.get("variant_id") != request.get("variant_id"):
                raise ValueError("Replace the selected variant explicitly")
            existing["quantity"] += request["quantity"]
        else:
            items.append({k: request[k] for k in ("product_id", "quantity", "variant_id") if k in request})
        cart["items"] = self._items(items)
        return self._total(cart)

    async def update_cart_item(self, request):
        cart = self._active(request["cart_id"])
        items = self._inputs(cart)
        existing = next((i for i in items if i["product_id"] == request["product_id"]), None)
        if existing is None:
            raise ValueError("Cart item not found")
        existing["quantity"] = request["quantity"]
        cart["items"] = self._items(items)
        return self._total(cart)
