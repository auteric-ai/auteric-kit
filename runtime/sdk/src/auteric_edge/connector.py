"""Manual SDK integration and a nonfinancial mock store."""

from abc import ABC, abstractmethod
from decimal import Decimal
from uuid import uuid4

from .models import validate_input, validate_output


class CommerceConnector(ABC):
    @abstractmethod
    async def search_products(self, request): ...
    @abstractmethod
    async def get_product(self, request): ...
    @abstractmethod
    async def create_cart(self, request): ...
    @abstractmethod
    async def get_cart(self, request): ...
    @abstractmethod
    async def add_to_cart(self, request): ...
    @abstractmethod
    async def update_cart_item(self, request): ...
    @abstractmethod
    async def remove_from_cart(self, request): ...
    @abstractmethod
    async def create_checkout(self, request): ...
    @abstractmethod
    async def get_checkout(self, request): ...

    async def replace_cart_items(self, request):
        raise NotImplementedError("Atomic cart replacement is not connected")

    async def cancel_cart(self, request):
        raise NotImplementedError("Cart cancellation is not connected")

    async def execute(self, operation, request, mapping=None):
        data = validate_input(operation, request)
        result = await getattr(self, operation)(data)
        return validate_output(operation, result)


class MockConnector(CommerceConnector):
    """In-memory demo state. Never use as a production merchant connector."""

    def __init__(self):
        self.products = {
            "demo-shoes": {
                "id": "demo-shoes",
                "sku": "DEMO-SHOES",
                "title": "Demo Shoes",
                "price": "100.00",
                "currency": "USD",
                "availability": "in_stock",
                "inventory": 4200,
            }
        }
        self.carts, self.checkouts = {}, {}

    async def search_products(self, request):
        return [p.copy() for p in self.products.values() if request["query"].lower() in p["title"].lower()][
            : request["limit"]
        ]

    async def get_product(self, request):
        return self.products[request["product_id"]].copy()

    async def create_cart(self, request):
        if request["currency"] != "USD":
            raise ValueError("Mock store supports USD only")
        row = {"id": str(uuid4()), "items": [], "subtotal": "0", "total": "0", "currency": "USD"}
        row["items"] = self._items(request.get("items", []))
        self._total(row)
        self.carts[row["id"]] = row
        return row

    async def get_cart(self, request):
        return self.carts[request["cart_id"]]

    def _total(self, cart):
        total = sum((Decimal(i["total_price"]) for i in cart["items"]), Decimal("0"))
        cart["subtotal"] = cart["total"] = str(total)
        return cart

    def _active(self, cart_id):
        row = self.carts[cart_id]
        if row.get("status") == "canceled":
            raise ValueError("Cart is canceled")
        return row

    def _items(self, items):
        result, seen = [], set()
        for item in items:
            product = self.products[item["product_id"]]
            if (
                item["product_id"] in seen
                or item.get("variant_id") is not None
                or item["quantity"] > product["inventory"]
            ):
                raise ValueError("Duplicate item, unavailable variant or insufficient inventory")
            seen.add(item["product_id"])
            result.append(
                {
                    "product_id": product["id"],
                    "sku": product["sku"],
                    "quantity": item["quantity"],
                    "unit_price": product["price"],
                    "total_price": str(Decimal(product["price"]) * item["quantity"]),
                }
            )
        return result

    async def replace_cart_items(self, request):
        cart = self._active(request["cart_id"])
        items = self._items(request["items"])
        cart["items"] = items
        return self._total(cart)

    async def cancel_cart(self, request):
        cart = self.carts[request["cart_id"]]
        cart["status"] = "canceled"
        return cart

    async def add_to_cart(self, request):
        cart, product = self._active(request["cart_id"]), self.products[request["product_id"]]
        item = next((i for i in cart["items"] if i["product_id"] == product["id"]), None)
        quantity = request["quantity"] + (item["quantity"] if item else 0)
        if request.get("variant_id") is not None or quantity > product["inventory"]:
            raise ValueError("Variant unavailable or insufficient stock")
        if item is None:
            item = {
                "product_id": product["id"],
                "sku": product["sku"],
                "quantity": quantity,
                "unit_price": product["price"],
                "total_price": "0",
            }
            cart["items"].append(item)
        item["quantity"], item["total_price"] = quantity, str(Decimal(product["price"]) * quantity)
        return self._total(cart)

    async def update_cart_item(self, request):
        cart = self._active(request["cart_id"])
        item = next(i for i in cart["items"] if i["product_id"] == request["product_id"])
        if request["quantity"] > self.products[request["product_id"]]["inventory"]:
            raise ValueError("Insufficient stock")
        item["quantity"] = request["quantity"]
        item["total_price"] = str(Decimal(item["unit_price"]) * item["quantity"])
        return self._total(cart)

    async def remove_from_cart(self, request):
        cart = self._active(request["cart_id"])
        cart["items"] = [i for i in cart["items"] if i["product_id"] != request["product_id"]]
        return self._total(cart)

    async def create_checkout(self, request):
        cart = self._active(request["cart_id"])
        if not cart["items"]:
            raise ValueError("Cannot check out an empty cart")
        row = {
            "id": str(uuid4()),
            "cart_id": cart["id"],
            "status": "incomplete",
            "total": cart["total"],
            "currency": cart["currency"],
            "checkout_url": "https://example.com/demo-checkout",
            "metadata": {"demo": True, "payment_execution": False},
        }
        self.checkouts[row["id"]] = row
        return row

    async def get_checkout(self, request):
        return self.checkouts[request["checkout_id"]]
