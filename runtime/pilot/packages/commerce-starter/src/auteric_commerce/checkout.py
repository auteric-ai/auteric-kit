"""Sandbox checkout/payment bridge: genuine merchant-side checkout lifecycle.

This module is the reference merchant runtime for the 20-operation commerce
surface. It implements the checkout state machine (draft -> ready -> processing
-> completed, with canceled/expired/requires_action branches), expiring
fulfillment quotes, discount eligibility recalculation, and a sandbox payment
handler interface with a fake PSP adapter.

Payment truth lives here, merchant-side:

- ``FakePSP.authorize`` only ever returns *pending*; capture is confirmed by a
  signed webhook (or one authoritative reconciliation read after an uncertain
  response). Duplicate or out-of-order webhooks are acknowledged without side
  effects: no double charge, no double order.
- A payment response lost after PSP capture raises :class:`PaymentUncertain`;
  ``reconcile_payment`` settles it exactly once and never re-authorizes.
- Without a configured payment handler, completion is an explicit merchant
  handoff (``requires_action`` + checkout URL). It is never reported completed.
- Order creation is keyed to the checkout and happens exactly once, only after
  stock is confirmed and decremented atomically at payment success.

All state is in-memory and all data synthetic; nothing here contacts a real
payment processor or merchant platform.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from decimal import Decimal
from uuid import uuid4

ZERO = Decimal("0")
TWO_PLACES = Decimal("0.01")

# Checkout lifecycle states. The canonical wire schema maps these onto its own
# bounded vocabulary in the connector adapter; the engine keeps the full set.
DRAFT = "draft"
READY = "ready"
PROCESSING = "processing"
REQUIRES_ACTION = "requires_action"
COMPLETED = "completed"
CANCELED = "canceled"
EXPIRED = "expired"
TERMINAL = {COMPLETED, CANCELED, EXPIRED}


class PaymentUncertain(TimeoutError):
    """The PSP may have captured but its response was lost. Reconcile, never retry."""


def _money(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < ZERO:
        raise ValueError("Invalid money amount")
    return amount.quantize(TWO_PLACES)


def _text(amount: Decimal) -> str:
    return str(amount.quantize(TWO_PLACES))


class FakePSP:
    """Deterministic fake payment service provider with a signed-webhook flow.

    ``authorize`` records one capture intent per payment id and returns
    ``pending``. ``behavior`` may name a fault for the next authorize:
    ``"timeout_after_capture"`` drops the response after capturing (the caller
    sees :class:`TimeoutError` while the ledger holds the capture), and
    ``"decline"`` marks the payment declined so the webhook settles it as such.
    The ``ledger`` is the audit trail: one ``capture`` per successful authorize,
    one ``refund`` per voided capture.
    """

    def __init__(self, secret="sandbox-psp-secret"):
        self.secret = secret.encode()
        self.payments = {}
        self.ledger = []
        self.behavior = None

    def authorize(self, payment_id, amount, currency):
        amount = _money(amount)
        existing = self.payments.get(payment_id)
        if existing:
            # Authorize is keyed by payment id: a repeated call is a replay of
            # the same intent, never a second capture.
            if existing["amount"] != amount or existing["currency"] != currency:
                raise ValueError("Payment id replayed with a different amount")
            return {"payment_id": payment_id, "state": "pending"}
        behavior, self.behavior = self.behavior, None
        record = {"id": payment_id, "amount": amount, "currency": currency,
                  "state": "declined" if behavior == "decline" else "captured"}
        self.payments[payment_id] = record
        if record["state"] == "captured":
            self.ledger.append({"event": "capture", "payment_id": payment_id, "amount": _text(amount)})
        if behavior == "timeout_after_capture":
            raise TimeoutError("PSP response lost after capture")
        return {"payment_id": payment_id, "state": "pending"}

    def void(self, payment_id):
        record = self.payments[payment_id]
        if record["state"] != "captured":
            raise ValueError("Only a captured payment can be refunded")
        record["state"] = "refunded"
        self.ledger.append({"event": "refund", "payment_id": payment_id, "amount": _text(record["amount"])})
        return {"payment_id": payment_id, "state": "refunded"}

    def status(self, payment_id):
        """Authoritative reconciliation read; never mutates, never charges."""
        record = self.payments.get(payment_id)
        if not record:
            return {"payment_id": payment_id, "state": "not_found"}
        return {"payment_id": payment_id, "state": record["state"]}

    def sign(self, payload) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def webhook(self, payment_id, event, amount, currency):
        """Build a signed webhook delivery for the sandbox harness."""
        payload = {"payment_id": payment_id, "event": event,
                   "amount": _text(_money(amount)), "currency": currency}
        return payload, self.sign(payload)

    def verify(self, payload, signature) -> bool:
        return hmac.compare_digest(self.sign(payload), signature or "")


class SandboxPaymentHandler:
    """Payment handler interface bound to one FakePSP for one storefront."""

    def __init__(self, psp: FakePSP):
        self.psp = psp
        self.handler_id = "sandbox"

    def authorize(self, payment_id, amount, currency):
        return self.psp.authorize(payment_id, amount, currency)

    def verify_webhook(self, payload, signature) -> bool:
        return self.psp.verify(payload, signature)

    def status(self, payment_id):
        return self.psp.status(payment_id)

    def void(self, payment_id):
        return self.psp.void(payment_id)


class SandboxStorefront:
    """Merchant-side cart/checkout/order engine with expiring quotes.

    ``clock`` is injectable so tests can advance time past checkout and quote
    expiry. ``payment_handler=None`` models a merchant that only supports the
    hosted-payment handoff.
    """

    def __init__(self, products=None, payment_handler: SandboxPaymentHandler | None = None,
                 *, clock=time.time, checkout_ttl=900, quote_ttl=300,
                 free_shipping_threshold=Decimal("100.00"), currency="USD",
                 checkout_base_url="https://merchant.example/checkout"):
        self.clock = clock
        self.checkout_ttl = checkout_ttl
        self.quote_ttl = quote_ttl
        self.free_shipping_threshold = _money(free_shipping_threshold)
        self.currency = currency
        self.checkout_base_url = checkout_base_url.rstrip("/")
        self.payment_handler = payment_handler
        self.lock = threading.RLock()
        self.products = products or {
            "demo-shoes": {"id": "demo-shoes", "sku": "DEMO-SHOES", "title": "Demo Shoes",
                           "price": "100.00", "currency": currency, "availability": "in_stock",
                           "inventory": 10},
            "demo-laces": {"id": "demo-laces", "sku": "DEMO-LACES", "title": "Demo Laces",
                           "price": "20.00", "currency": currency, "availability": "in_stock",
                           "inventory": 50},
        }
        self.carts = {}
        self.checkouts = {}
        self.orders = {}

    # -- catalog -----------------------------------------------------------

    def search_products(self, query="", limit=20):
        items = [p.copy() for p in self.products.values() if query.lower() in p["title"].lower()]
        return items[:limit]

    def get_product(self, product_id):
        return self.products[product_id].copy()

    # -- carts -------------------------------------------------------------

    def create_cart(self, items=(), currency=None):
        currency = currency or self.currency
        if currency != self.currency:
            raise ValueError("Unsupported currency")
        with self.lock:
            cart = {"id": "cart_" + uuid4().hex, "currency": currency, "items": [],
                    "discount_codes": [], "revision": 0, "status": "active"}
            self._set_items(cart, items)
            self.carts[cart["id"]] = cart
            return self._cart_view(cart)

    def _cart(self, cart_id):
        cart = self.carts[cart_id]
        if cart["status"] == "canceled":
            raise ValueError("Cart is canceled")
        return cart

    def _set_items(self, cart, items):
        seen = set()
        lines = []
        for item in items:
            product = self.products[item["product_id"]]
            if item["product_id"] in seen:
                raise ValueError("Duplicate item identifiers are not supported")
            seen.add(item["product_id"])
            quantity = item["quantity"]
            if type(quantity) is not int or quantity < 1:
                raise ValueError("Quantity must be a positive integer")
            if product["availability"] != "in_stock" or quantity > product["inventory"]:
                raise ValueError("Insufficient inventory or unavailable product")
            lines.append({"product_id": product["id"], "sku": product["sku"], "quantity": quantity,
                          "unit_price": _text(_money(product["price"])),
                          "total_price": _text(_money(product["price"]) * quantity)})
        cart["items"] = lines
        cart["revision"] += 1

    def _subtotal(self, cart):
        return sum((_money(line["total_price"]) for line in cart["items"]), ZERO)

    def _discount(self, cart):
        """Eligibility is recomputed from current items on every read/mutation."""
        total = ZERO
        for code in cart["discount_codes"]:
            if code == "WELCOME10":
                total += (self._subtotal(cart) * Decimal("0.10")).quantize(TWO_PLACES)
            # FREESHIP is a fulfillment discount, applied to the shipping quote.
        return min(total, self._subtotal(cart))

    def _cart_view(self, cart):
        subtotal = self._subtotal(cart)
        discount = self._discount(cart)
        return {"id": cart["id"], "currency": cart["currency"],
                "items": [dict(line) for line in cart["items"]],
                "subtotal": _text(subtotal), "discounts": _text(discount), "tax": "0.00",
                "total": _text(subtotal - discount), "status": cart["status"],
                "revision": cart["revision"],
                "metadata": {"discount_codes": list(cart["discount_codes"])}}

    def get_cart(self, cart_id):
        with self.lock:
            return self._cart_view(self.carts[cart_id])

    def add_to_cart(self, cart_id, product_id, quantity):
        with self.lock:
            cart = self._cart(cart_id)
            current = {line["product_id"]: line["quantity"] for line in cart["items"]}
            current[product_id] = current.get(product_id, 0) + quantity
            self._set_items(cart, [{"product_id": pid, "quantity": qty} for pid, qty in current.items()])
            return self._cart_view(cart)

    def update_cart_item(self, cart_id, product_id, quantity):
        with self.lock:
            cart = self._cart(cart_id)
            if product_id not in {line["product_id"] for line in cart["items"]}:
                raise ValueError("Cart item not found")
            self._set_items(cart, [{"product_id": line["product_id"],
                                    "quantity": quantity if line["product_id"] == product_id else line["quantity"]}
                                   for line in cart["items"]])
            return self._cart_view(cart)

    def remove_from_cart(self, cart_id, product_id):
        with self.lock:
            cart = self._cart(cart_id)
            self._set_items(cart, [{"product_id": line["product_id"], "quantity": line["quantity"]}
                                   for line in cart["items"] if line["product_id"] != product_id])
            return self._cart_view(cart)

    def replace_cart_items(self, cart_id, items):
        with self.lock:
            cart = self._cart(cart_id)
            self._set_items(cart, items)
            return self._cart_view(cart)

    def cancel_cart(self, cart_id):
        with self.lock:
            cart = self.carts[cart_id]
            cart["status"] = "canceled"
            cart["revision"] += 1
            return self._cart_view(cart)

    def apply_discount_code(self, cart_id, code):
        with self.lock:
            cart = self._cart(cart_id)
            if code not in {"WELCOME10", "FREESHIP"}:
                raise ValueError("Unknown or ineligible discount code")
            if code not in cart["discount_codes"]:
                cart["discount_codes"].append(code)
                cart["revision"] += 1
            return self._cart_view(cart)

    def remove_discount_code(self, cart_id, code):
        with self.lock:
            cart = self._cart(cart_id)
            cart["discount_codes"] = [c for c in cart["discount_codes"] if c != code]
            cart["revision"] += 1
            return self._cart_view(cart)

    # -- fulfillment quotes -------------------------------------------------

    def get_shipping_options(self, cart_id, address=None):
        """Issue quotes bound to the exact address; every quote carries expiry.

        Issued quotes replace the cart's current quote set, so reading options
        is the only way to obtain a selectable quote.
        """
        with self.lock:
            cart = self._cart(cart_id)
            address = address or {}
            digest = hashlib.sha256(json.dumps(address, sort_keys=True).encode()).hexdigest()[:16]
            expires = self.clock() + self.quote_ttl
            base = [("standard", "Standard shipping", Decimal("5.00")),
                    ("express", "Express shipping", Decimal("15.00"))]
            quotes = [{"id": f"ship_{name}_{digest}_{int(expires)}", "option": name, "title": title,
                       "amount": _text(amount), "currency": cart["currency"],
                       "expires_at": expires, "address_digest": digest} for name, title, amount in base]
            cart["quotes"] = {quote["id"]: quote for quote in quotes}
            return quotes

    def _quote(self, checkout, option_id):
        cart = self.carts[checkout["cart_id"]]
        quote = cart.get("quotes", {}).get(option_id)
        if not quote:
            raise ValueError("Unknown or stale shipping quote; read current shipping options")
        if quote["expires_at"] <= self.clock():
            raise ValueError("Shipping quote expired; read current shipping options")
        if quote["address_digest"] != checkout.get("address_digest"):
            raise ValueError("Shipping quote predates the current address; read current shipping options")
        return quote

    # -- checkout state machine ----------------------------------------------

    def create_checkout(self, cart_id, buyer=None, context=None, fulfillment=None):
        with self.lock:
            cart = self._cart(cart_id)
            if not cart["items"]:
                raise ValueError("Cannot check out an empty cart")
            checkout = {"id": "chk_" + uuid4().hex, "cart_id": cart["id"], "state": DRAFT,
                        "revision": 1, "expires_at": self.clock() + self.checkout_ttl,
                        "buyer": buyer or {}, "context": context or {},
                        "address": None, "address_digest": None, "quotes": {}, "quote": None,
                        "payment": None, "order_id": None, "handoff": None,
                        "created_at": self.clock()}
            self.checkouts[checkout["id"]] = checkout
            return self._checkout_view(checkout)

    def _checkout(self, checkout_id):
        checkout = self.checkouts[checkout_id]
        if checkout["state"] in {DRAFT, READY} and checkout["expires_at"] <= self.clock():
            checkout["state"] = EXPIRED
        return checkout

    def _shipping(self, checkout):
        if not checkout.get("quote"):
            return ZERO
        quote = checkout["quote"]
        cart = self.carts[checkout["cart_id"]]
        if "FREESHIP" in cart["discount_codes"] and self._subtotal(cart) >= self.free_shipping_threshold:
            return ZERO
        return _money(quote["amount"])

    def _total(self, checkout):
        cart = self.carts[checkout["cart_id"]]
        return self._subtotal(cart) - self._discount(cart) + self._shipping(checkout)

    def _checkout_view(self, checkout):
        cart = self.carts[checkout["cart_id"]]
        view = {"id": checkout["id"], "cart_id": checkout["cart_id"], "state": checkout["state"],
                "revision": checkout["revision"], "currency": cart["currency"],
                "total": _text(self._total(checkout)), "expires_at": checkout["expires_at"],
                "buyer": dict(checkout["buyer"]),
                "shipping_address": dict(checkout["address"]) if checkout["address"] else None,
                "selected_shipping": dict(checkout["quote"]) if checkout["quote"] else None,
                "payment": dict(checkout["payment"]) if checkout["payment"] else None,
                "order_id": checkout["order_id"],
                "checkout_url": f"{self.checkout_base_url}/{checkout['id']}"}
        if checkout["handoff"]:
            view["handoff"] = dict(checkout["handoff"])
        return view

    def get_checkout(self, checkout_id):
        with self.lock:
            return self._checkout_view(self._checkout(checkout_id))

    def set_shipping_address(self, checkout_id, address):
        with self.lock:
            checkout = self._checkout(checkout_id)
            if checkout["state"] in TERMINAL | {REQUIRES_ACTION}:
                raise ValueError("Checkout is not editable after a terminal state or merchant handoff")
            if not isinstance(address, dict) or not address.get("country"):
                raise ValueError("A shipping address with a country is required")
            checkout["address"] = dict(address)
            # Address change invalidates every quote issued for the old address.
            checkout["address_digest"] = hashlib.sha256(
                json.dumps(address, sort_keys=True).encode()).hexdigest()[:16]
            checkout["quote"] = None
            checkout["revision"] += 1
            if checkout["state"] == READY:
                checkout["state"] = DRAFT
            return self._checkout_view(checkout)

    def select_shipping_option(self, checkout_id, option_id):
        with self.lock:
            checkout = self._checkout(checkout_id)
            if checkout["state"] in TERMINAL | {REQUIRES_ACTION}:
                raise ValueError("Checkout is not editable after a terminal state or merchant handoff")
            if not checkout.get("address"):
                raise ValueError("Set the shipping address before selecting a quote")
            checkout["quote"] = self._quote(checkout, option_id)
            checkout["state"] = READY
            checkout["revision"] += 1
            return self._checkout_view(checkout)

    def update_checkout(self, checkout_id, items=None, buyer=None, context=None, fulfillment=None):
        with self.lock:
            checkout = self._checkout(checkout_id)
            if checkout["state"] in TERMINAL | {REQUIRES_ACTION}:
                raise ValueError("Checkout is not editable after a terminal state or merchant handoff")
            if items is not None:
                self._set_items(self._cart(checkout["cart_id"]), items)
            if buyer is not None:
                checkout["buyer"] = dict(buyer)
            if context is not None:
                checkout["context"] = dict(context)
            checkout["revision"] += 1
            return self._checkout_view(checkout)

    def cancel_checkout(self, checkout_id):
        with self.lock:
            checkout = self._checkout(checkout_id)
            if checkout["state"] == COMPLETED:
                raise ValueError("Completed checkout cannot be canceled")
            if checkout["state"] in {CANCELED, EXPIRED}:
                raise ValueError("Checkout is already terminal")
            if checkout["payment"] and checkout["payment"]["state"] == "pending":
                raise ValueError("Checkout payment is in flight; reconcile before canceling")
            checkout["state"] = CANCELED
            checkout["revision"] += 1
            return self._checkout_view(checkout)

    # -- payment bridge -------------------------------------------------------

    def complete_checkout(self, checkout_id, payment):
        """Authorize server-computed totals through the configured handler.

        Requires: configured payment handler, explicit buyer authorization, the
        current resource revision, a non-terminal ready checkout. Any client
        supplied amount is ignored; the authoritative total is recomputed here.
        """
        with self.lock:
            checkout = self._checkout(checkout_id)
            if checkout["state"] in TERMINAL:
                raise ValueError("Checkout is terminal")
            if checkout["state"] == REQUIRES_ACTION:
                # A merchant-hosted handoff is a terminal boundary for the
                # agent's payment attempt.  Replaying the same request must
                # neither mint a fresh handoff nor advance the revision: that
                # could make a buyer-approved view appear stale while no
                # payment was attempted at all.
                if checkout["handoff"]:
                    return self._checkout_view(checkout)
                raise ValueError("Checkout awaits buyer action on the merchant site")
            if self.payment_handler is None:
                # Explicit merchant handoff. Never a fake completion.
                checkout["state"] = REQUIRES_ACTION
                checkout["revision"] += 1
                checkout["handoff"] = {
                    "kind": "merchant_hosted_payment",
                    "checkout_url": f"{self.checkout_base_url}/{checkout['id']}",
                    "reason": "payment_handler_not_configured",
                }
                return self._checkout_view(checkout)
            if checkout["state"] == PROCESSING:
                # Same checkout completion replayed while the PSP decision is
                # outstanding: report state, never authorize twice.
                return self._checkout_view(checkout)
            if checkout["state"] != READY:
                raise ValueError("Checkout is not ready; select a current shipping quote")
            if not payment.get("buyer_authorized"):
                raise ValueError("Buyer authorization is required to complete checkout")
            if payment.get("expected_revision") != checkout["revision"]:
                raise ValueError("Checkout changed since review; re-read before completing")
            total = self._total(checkout)  # authoritative, server-computed
            cart = self.carts[checkout["cart_id"]]
            payment_id = "pay_" + checkout["id"]
            checkout["payment"] = {"id": payment_id, "state": "authorizing",
                                   "amount": _text(total), "currency": cart["currency"],
                                   "handler": self.payment_handler.handler_id}
            try:
                receipt = self.payment_handler.authorize(payment_id, total, cart["currency"])
            except TimeoutError:
                checkout["payment"]["state"] = "uncertain"
                checkout["state"] = PROCESSING
                checkout["revision"] += 1
                raise PaymentUncertain(
                    "Payment response lost after authorization; reconcile by payment id") from None
            checkout["payment"]["state"] = receipt["state"]
            checkout["state"] = PROCESSING
            checkout["revision"] += 1
            return self._checkout_view(checkout)

    def _settle_success(self, checkout):
        """Stock-confirmed settlement; creates the order exactly once."""
        payment = checkout["payment"]
        cart = self.carts[checkout["cart_id"]]
        for line in cart["items"]:
            product = self.products[line["product_id"]]
            if product["availability"] != "in_stock" or product["inventory"] < line["quantity"]:
                # Payment captured but stock is gone: refund, never create an order.
                self.payment_handler.void(payment["id"])
                payment["state"] = "refunded"
                payment["reason"] = "insufficient_stock_at_completion"
                checkout["state"] = READY
                checkout["revision"] += 1
                return self._checkout_view(checkout)
        for line in cart["items"]:
            self.products[line["product_id"]]["inventory"] -= line["quantity"]
        payment["state"] = "succeeded"
        order_id = "order_" + checkout["id"]
        if order_id not in self.orders:
            self.orders[order_id] = {
                "id": order_id, "checkout_id": checkout["id"], "status": "confirmed",
                "items": [dict(line) for line in cart["items"]],
                "total": payment["amount"], "currency": payment["currency"],
                "order_url": f"https://merchant.example/orders/{order_id}",
                "metadata": {"payment_id": payment["id"], "payment_state": "succeeded"},
            }
        checkout["order_id"] = order_id
        checkout["state"] = COMPLETED
        checkout["revision"] += 1
        return self._checkout_view(checkout)

    def handle_payment_webhook(self, payload, signature):
        """Signed PSP webhook: pending -> succeeded/declined; anything else is a no-op."""
        handler = self.payment_handler
        if handler is None or not handler.verify_webhook(payload, signature):
            raise ValueError("Webhook signature is invalid")
        with self.lock:
            payment_id = payload.get("payment_id")
            checkout = next((c for c in self.checkouts.values()
                             if c["payment"] and c["payment"]["id"] == payment_id), None)
            if checkout is None:
                raise KeyError(payment_id)
            payment = checkout["payment"]
            if payment["state"] != "pending":
                # Duplicate or out-of-order delivery: acknowledge without
                # charging again, refunding again, or creating a second order.
                return self._checkout_view(checkout)
            if payload.get("amount") != payment["amount"] or payload.get("currency") != payment["currency"]:
                raise ValueError("Webhook amount differs from the authorized total")
            event = payload.get("event")
            if event == "succeeded":
                return self._settle_success(checkout)
            if event == "declined":
                payment["state"] = "declined"
                checkout["state"] = READY
                checkout["revision"] += 1
                return self._checkout_view(checkout)
            raise ValueError("Unsupported webhook event")

    def reconcile_payment(self, payment_id):
        """Settle an uncertain payment from one authoritative PSP read.

        Never re-authorizes. A payment that already settled (or was never
        uncertain) is returned unchanged, so reconciliation is safe to attempt
        but only ever transitions once.
        """
        handler = self.payment_handler
        if handler is None:
            raise ValueError("No payment handler is configured")
        with self.lock:
            checkout = next((c for c in self.checkouts.values()
                             if c["payment"] and c["payment"]["id"] == payment_id), None)
            if checkout is None:
                raise KeyError(payment_id)
            payment = checkout["payment"]
            if payment["state"] != "uncertain":
                return self._checkout_view(checkout)
            status = handler.status(payment_id)["state"]
            if status == "captured":
                payment["state"] = "pending"
                return self._settle_success(checkout)
            if status in {"declined", "not_found"}:
                payment["state"] = "declined"
                checkout["state"] = READY
                checkout["revision"] += 1
                return self._checkout_view(checkout)
            raise ValueError("PSP state is still ambiguous; do not retry the charge")

    # -- orders ----------------------------------------------------------------

    def get_order(self, order_id):
        with self.lock:
            order = self.orders[order_id]
            checkout = self.checkouts[order["checkout_id"]]
            view = dict(order)
            view["items"] = [dict(line) for line in order["items"]]
            # Payment state is read from the live payment record, not a copy.
            view["metadata"] = {**order["metadata"],
                                "payment_state": checkout["payment"]["state"] if checkout["payment"] else None}
            return view
