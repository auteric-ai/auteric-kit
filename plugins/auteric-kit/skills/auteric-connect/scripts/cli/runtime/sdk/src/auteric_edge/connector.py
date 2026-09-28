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

    async def update_checkout(self, request):
        raise NotImplementedError("Checkout update is not connected")

    async def complete_checkout(self, request):
        raise NotImplementedError("Checkout completion is not connected")

    async def cancel_checkout(self, request):
        raise NotImplementedError("Checkout cancellation is not connected")

    async def get_order(self, request):
        raise NotImplementedError("Order lookup is not connected")

    async def apply_discount_code(self, request):
        raise NotImplementedError("Discount application is not connected")

    async def remove_discount_code(self, request):
        raise NotImplementedError("Discount removal is not connected")

    async def get_shipping_options(self, request):
        raise NotImplementedError("Shipping option lookup is not connected")

    async def set_shipping_address(self, request):
        raise NotImplementedError("Shipping address update is not connected")

    async def select_shipping_option(self, request):
        raise NotImplementedError("Shipping option selection is not connected")

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
        self.carts, self.checkouts, self.orders = {}, {}, {}

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
        discount = min(Decimal(str(cart.get("discounts", "0"))), total)
        cart["subtotal"], cart["total"] = str(total), str(total - discount)
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

    async def update_checkout(self, request):
        checkout = self.checkouts[request["checkout_id"]]
        if checkout["status"] in {"completed", "canceled"}:
            raise ValueError("Checkout is terminal")
        cart = self._active(checkout["cart_id"])
        cart["items"] = self._items(request.get("items", []))
        self._total(cart)
        checkout.update(
            total=cart["total"], status="ready_for_complete",
            customer_context=request.get("buyer", {}),
            shipping_context={"context": request.get("context", {}), "fulfillment": request.get("fulfillment", {})},
        )
        return checkout

    async def complete_checkout(self, request):
        checkout = self.checkouts[request["checkout_id"]]
        if checkout["status"] != "ready_for_complete" or not request.get("payment"):
            raise ValueError("Checkout is not ready or payment is absent")
        order_id = str(uuid4())
        checkout["status"] = "completed"
        checkout.setdefault("metadata", {})["order_id"] = order_id
        cart = self.carts[checkout["cart_id"]]
        self.orders[order_id] = {
            "id": order_id, "checkout_id": checkout["id"], "status": "confirmed",
            "items": [item.copy() for item in cart["items"]], "total": checkout["total"],
            "currency": checkout["currency"], "order_url": "https://example.com/demo-order/" + order_id,
        }
        return checkout

    async def cancel_checkout(self, request):
        checkout = self.checkouts[request["checkout_id"]]
        if checkout["status"] == "completed":
            raise ValueError("Completed checkout cannot be canceled")
        checkout["status"] = "canceled"
        return checkout

    async def get_order(self, request):
        return self.orders[request["order_id"]]

    async def apply_discount_code(self, request):
        cart = self._active(request["cart_id"])
        codes = cart.setdefault("metadata", {}).setdefault("discount_codes", [])
        if request["code"] not in codes:
            codes.append(request["code"])
        cart["discounts"] = "10.00" if codes else "0"
        return self._total(cart)

    async def remove_discount_code(self, request):
        cart = self._active(request["cart_id"])
        codes = cart.setdefault("metadata", {}).setdefault("discount_codes", [])
        cart["metadata"]["discount_codes"] = [code for code in codes if code != request["code"]]
        cart["discounts"] = "10.00" if cart["metadata"]["discount_codes"] else "0"
        return self._total(cart)

    async def get_shipping_options(self, request):
        self._active(request["cart_id"])
        return [{"id": "standard", "title": "Standard shipping", "amount": 500, "currency": "USD"}]

    async def set_shipping_address(self, request):
        checkout = self.checkouts[request["checkout_id"]]
        checkout["shipping_context"] = {**checkout.get("shipping_context", {}), "address": request["address"]}
        return checkout

    async def select_shipping_option(self, request):
        checkout = self.checkouts[request["checkout_id"]]
        checkout["shipping_context"] = {**checkout.get("shipping_context", {}), "selected_option_id": request["option_id"]}
        checkout["status"] = "ready_for_complete"
        return checkout
