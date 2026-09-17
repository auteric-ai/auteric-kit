"""Version 1 canonical commerce schema; merchant objects need not use these fields."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=2)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Product(Model):
    id: Identifier
    sku: Identifier
    title: str = Field(min_length=1, max_length=1000)
    description: str | None = None
    price: Money
    currency: Currency
    availability: Literal["in_stock", "out_of_stock", "preorder", "unknown"] = "unknown"
    inventory: int | None = Field(default=None, ge=0)
    variants: list[dict[str, JsonValue]] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    product_url: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class CartItem(Model):
    product_id: Identifier
    variant_id: Identifier | None = None
    sku: Identifier
    quantity: int = Field(ge=1, le=10000, strict=True)
    unit_price: Money
    total_price: Money


class Cart(Model):
    id: Identifier
    items: list[CartItem] = Field(default_factory=list)
    subtotal: Money
    discounts: Money = Decimal("0")
    tax: Money = Decimal("0")
    total: Money
    currency: Currency
    status: Literal["active", "canceled"] = "active"
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class Checkout(Model):
    id: Identifier
    cart_id: Identifier
    status: Literal["incomplete", "ready_for_complete", "completed", "canceled", "requires_escalation"]
    checkout_url: str | None = None
    total: Money
    currency: Currency
    customer_context: dict[str, JsonValue] = Field(default_factory=dict)
    shipping_context: dict[str, JsonValue] = Field(default_factory=dict)
    payment_context: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SearchRequest(Model):
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class ProductRequest(Model):
    product_id: Identifier


class ItemInput(Model):
    product_id: Identifier
    variant_id: Identifier | None = None
    quantity: int = Field(ge=1, le=10000, strict=True)


class CreateCartRequest(Model):
    currency: Currency = "USD"
    items: list[ItemInput] = Field(default_factory=list, max_length=100)


class CartRequest(Model):
    cart_id: Identifier


class AddRequest(CartRequest):
    product_id: Identifier
    variant_id: Identifier | None = None
    quantity: int = Field(ge=1, le=10000, strict=True)


class UpdateRequest(CartRequest):
    product_id: Identifier
    quantity: int = Field(ge=1, le=10000, strict=True)


class RemoveRequest(CartRequest):
    product_id: Identifier


class ReplaceRequest(CartRequest):
    items: list[ItemInput] = Field(max_length=100)


class CheckoutRequest(Model):
    checkout_id: Identifier


INPUTS = {
    "search_products": SearchRequest,
    "get_product": ProductRequest,
    "create_cart": CreateCartRequest,
    "get_cart": CartRequest,
    "add_to_cart": AddRequest,
    "update_cart_item": UpdateRequest,
    "remove_from_cart": RemoveRequest,
    "create_checkout": CartRequest,
    "get_checkout": CheckoutRequest,
}
INPUTS.update({"replace_cart_items": ReplaceRequest, "cancel_cart": CartRequest})
OUTPUTS = {
    "search_products": TypeAdapter(list[Product]),
    "get_product": TypeAdapter(Product),
    **{
        k: TypeAdapter(Cart) for k in ("create_cart", "get_cart", "add_to_cart", "update_cart_item", "remove_from_cart")
    },
    "create_checkout": TypeAdapter(Checkout),
    "get_checkout": TypeAdapter(Checkout),
}
OUTPUTS.update({"replace_cart_items": TypeAdapter(Cart), "cancel_cart": TypeAdapter(Cart)})
READ_OPERATIONS = frozenset({"search_products", "get_product", "get_cart", "get_checkout"})


def validate_input(operation, data):
    if operation not in INPUTS:
        raise ValueError("Unsupported canonical operation")
    return INPUTS[operation].model_validate(data).model_dump(mode="json")


def validate_output(operation, data):
    if operation not in OUTPUTS:
        raise ValueError("Unsupported canonical operation")
    adapter = OUTPUTS[operation]
    return adapter.dump_python(adapter.validate_python(data), mode="json")
