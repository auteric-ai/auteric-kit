export declare const REGISTRY_DIGEST = "sha256:671bd2cb706dd60773de5fdecbe5d35a5822a0d33fafdcfb92f62ddd33134c0c";
export declare const REGISTRY_VERSION = "1.0.0";
export declare const MERCHANT_PROTOCOL = "1";
export declare class ContractValidationError extends Error {
    readonly category: string;
    readonly path: string;
    constructor(category: string, path: string, message: string);
}
export declare const CURRENCY_TABLE_VERSION = "iso4217:2015-amendment-174";
export declare const DEFAULT_CURRENCY_EXPONENT = 2;
export declare const CURRENCY_EXPONENTS: Readonly<Record<string, number>>;
export interface AddToCartInput {
    expected_revision?: number;
    product_id: IdsProductId;
    quantity: number;
    variant_id?: IdsVariantId;
}
export interface Address {
    city: string;
    country_code: string;
    line1: string;
    line2?: string;
    phone?: string;
    postal_code: string;
    recipient_name?: string;
    region?: string;
}
export interface ApplyDiscountCodeInput {
    code: IdsDiscountCode;
    expected_revision?: number;
}
export interface CancelCartInput {
    expected_revision?: number;
    reason?: string;
}
export interface CancelCheckoutInput {
    expected_revision?: number;
    reason?: string;
}
export interface Cart {
    cart_id: IdsCartId;
    currency: string;
    discount_total: Money;
    discounts: Discount[];
    line_items: LineItem[];
    resource_revision: number;
    status: "active" | "cancelled" | "converted";
    subtotal: Money;
    total: Money;
}
export interface Checkout {
    buyer_principal?: IdsPrincipalId;
    cart_id: IdsCartId;
    checkout_id: IdsCheckoutId;
    discount_total: Money;
    payment: CheckoutPayment;
    resource_revision: number;
    shipping_address?: Address;
    shipping_option?: ShippingOption;
    shipping_total: Money;
    status: "cancelled" | "completed" | "completing" | "payment_pending" | "pending" | "ready";
    subtotal: Money;
    tax_total: Money;
    total: Money;
}
export interface CheckoutPayment {
    handler?: string;
    status: "authorized" | "failed" | "not_started" | "paid" | "pending";
}
export interface CompleteCheckoutInput {
    expected_revision?: number;
    payment: CompleteCheckoutInputPayment;
}
export interface CompleteCheckoutInputPayment {
    handler: string;
    token: string;
}
export interface CompleteCheckoutOutput {
    order: Order;
}
export interface CreateCartInput {
    currency: string;
    line_items?: LineItemInput[];
}
export interface CreateCheckoutInput {
    buyer_principal?: IdsPrincipalId;
    cart_id: IdsCartId;
    expected_cart_revision?: number;
}
export interface Discount {
    amount: Money;
    code: IdsDiscountCode;
    description?: string;
    kind: "fixed_amount" | "free_shipping" | "percentage";
}
export interface Error {
    error: ErrorError;
}
export interface ErrorError {
    action_id: IdsActionId;
    code: "CAPABILITY_DISABLED" | "CONTRACT_MISMATCH" | "EXECUTION_UNCERTAIN" | "FORBIDDEN" | "IDEMPOTENCY_CONFLICT" | "INVALID_INPUT" | "OUT_OF_STOCK" | "PAYMENT_PENDING" | "RATE_LIMITED" | "RESOURCE_NOT_FOUND" | "REVISION_CONFLICT" | "SSRF_BLOCKED" | "UNAUTHENTICATED" | "UPSTREAM_ERROR";
    details?: Record<string, unknown>;
    message: string;
    retryable: boolean;
}
export interface GetCartInput {
}
export interface GetCheckoutInput {
}
export interface GetOrderInput {
}
export interface GetProductInput {
}
export interface GetShippingOptionsInput {
    currency?: string;
}
export interface GetShippingOptionsOutput {
    options: ShippingOption[];
}
export interface LineItem {
    line_id: IdsLineId;
    product_id: IdsProductId;
    quantity: number;
    title: string;
    total_price: Money;
    unit_price: Money;
    variant_id?: IdsVariantId;
}
export interface LineItemInput {
    product_id: IdsProductId;
    quantity: number;
    variant_id?: IdsVariantId;
}
export interface Money {
    amount_minor: number;
    currency: string;
}
export interface Order {
    buyer_principal: IdsBuyerPrincipalId;
    cart_id: IdsCartId;
    checkout_id: IdsCheckoutId;
    created_at: string;
    discount_total: Money;
    discounts: Discount[];
    line_items: LineItem[];
    order_id: IdsOrderId;
    shipping_address: Address;
    shipping_option: ShippingOption;
    shipping_total: Money;
    status: "cancelled" | "confirmed" | "fulfilled";
    subtotal: Money;
    tax_total: Money;
    total: Money;
}
export interface Pagination {
    has_more: boolean;
    next_cursor?: IdsCursor;
}
export interface Product {
    description?: string;
    product_id: IdsProductId;
    tags?: string[];
    title: string;
    variants: ProductVariantsItem[];
    vendor?: string;
}
export interface ProductVariantsItem {
    available: boolean;
    price: Money;
    title?: string;
    variant_id: IdsVariantId;
}
export interface RemoveDiscountCodeInput {
    expected_revision?: number;
}
export interface RemoveFromCartInput {
    expected_revision?: number;
}
export interface ReplaceCartItemsInput {
    expected_revision?: number;
    items: LineItemInput[];
}
export interface SearchProductsInput {
    category?: string;
    currency?: string;
    cursor?: IdsCursor;
    limit?: number;
    price_max_minor?: number;
    price_min_minor?: number;
    q: string;
}
export interface SearchProductsOutput {
    page: Pagination;
    results: Product[];
}
export interface SelectShippingOptionInput {
    expected_revision?: number;
    option_id: IdsShippingOptionId;
}
export interface SetShippingAddressInput {
    address: Address;
    expected_revision?: number;
}
export interface ShippingOption {
    carrier?: string;
    eta_days_max?: number;
    eta_days_min?: number;
    label: string;
    option_id: IdsShippingOptionId;
    price: Money;
}
export interface UpdateCartItemInput {
    expected_revision?: number;
    quantity: number;
}
export interface UpdateCheckoutInput {
    buyer_principal?: IdsPrincipalId;
    expected_revision?: number;
    shipping_address?: Address;
}
export type AddToCartOutput = Cart;
export type ApplyDiscountCodeOutput = Cart;
export type CancelCartOutput = Cart;
export type CancelCheckoutOutput = Checkout;
export type CreateCartOutput = Cart;
export type CreateCheckoutOutput = Checkout;
export type GetCartOutput = Cart;
export type GetCheckoutOutput = Checkout;
export type GetOrderOutput = Order;
export type GetProductOutput = Product;
export type IdsActionId = string;
export type IdsBuyerPrincipalId = string;
export type IdsCartId = string;
export type IdsCheckoutId = string;
export type IdsCursor = string;
export type IdsDiscountCode = string;
export type IdsLineId = string;
export type IdsOrderId = string;
export type IdsPrincipalId = string;
export type IdsProductId = string;
export type IdsShippingOptionId = string;
export type IdsVariantId = string;
export type RemoveDiscountCodeOutput = Cart;
export type RemoveFromCartOutput = Cart;
export type ReplaceCartItemsOutput = Cart;
export type SelectShippingOptionOutput = Checkout;
export type SetShippingAddressOutput = Checkout;
export type UpdateCartItemOutput = Cart;
export type UpdateCheckoutOutput = Checkout;
export declare function validateAddToCartInput(value: unknown, path?: string): void;
export declare function validateAddToCartOutput(value: unknown, path?: string): void;
export declare function validateAddress(value: unknown, path?: string): void;
export declare function validateApplyDiscountCodeInput(value: unknown, path?: string): void;
export declare function validateApplyDiscountCodeOutput(value: unknown, path?: string): void;
export declare function validateCancelCartInput(value: unknown, path?: string): void;
export declare function validateCancelCartOutput(value: unknown, path?: string): void;
export declare function validateCancelCheckoutInput(value: unknown, path?: string): void;
export declare function validateCancelCheckoutOutput(value: unknown, path?: string): void;
export declare function validateCart(value: unknown, path?: string): void;
export declare function validateCheckout(value: unknown, path?: string): void;
export declare function validateCompleteCheckoutInput(value: unknown, path?: string): void;
export declare function validateCompleteCheckoutOutput(value: unknown, path?: string): void;
export declare function validateCreateCartInput(value: unknown, path?: string): void;
export declare function validateCreateCartOutput(value: unknown, path?: string): void;
export declare function validateCreateCheckoutInput(value: unknown, path?: string): void;
export declare function validateCreateCheckoutOutput(value: unknown, path?: string): void;
export declare function validateDiscount(value: unknown, path?: string): void;
export declare function validateError(value: unknown, path?: string): void;
export declare function validateGetCartInput(value: unknown, path?: string): void;
export declare function validateGetCartOutput(value: unknown, path?: string): void;
export declare function validateGetCheckoutInput(value: unknown, path?: string): void;
export declare function validateGetCheckoutOutput(value: unknown, path?: string): void;
export declare function validateGetOrderInput(value: unknown, path?: string): void;
export declare function validateGetOrderOutput(value: unknown, path?: string): void;
export declare function validateGetProductInput(value: unknown, path?: string): void;
export declare function validateGetProductOutput(value: unknown, path?: string): void;
export declare function validateGetShippingOptionsInput(value: unknown, path?: string): void;
export declare function validateGetShippingOptionsOutput(value: unknown, path?: string): void;
export declare function validateIdsActionId(value: unknown, path?: string): void;
export declare function validateIdsBuyerPrincipalId(value: unknown, path?: string): void;
export declare function validateIdsCartId(value: unknown, path?: string): void;
export declare function validateIdsCheckoutId(value: unknown, path?: string): void;
export declare function validateIdsCursor(value: unknown, path?: string): void;
export declare function validateIdsDiscountCode(value: unknown, path?: string): void;
export declare function validateIdsLineId(value: unknown, path?: string): void;
export declare function validateIdsOrderId(value: unknown, path?: string): void;
export declare function validateIdsPrincipalId(value: unknown, path?: string): void;
export declare function validateIdsProductId(value: unknown, path?: string): void;
export declare function validateIdsShippingOptionId(value: unknown, path?: string): void;
export declare function validateIdsVariantId(value: unknown, path?: string): void;
export declare function validateLineItem(value: unknown, path?: string): void;
export declare function validateLineItemInput(value: unknown, path?: string): void;
export declare function validateMoney(value: unknown, path?: string): void;
export declare function validateOrder(value: unknown, path?: string): void;
export declare function validatePagination(value: unknown, path?: string): void;
export declare function validateProduct(value: unknown, path?: string): void;
export declare function validateRemoveDiscountCodeInput(value: unknown, path?: string): void;
export declare function validateRemoveDiscountCodeOutput(value: unknown, path?: string): void;
export declare function validateRemoveFromCartInput(value: unknown, path?: string): void;
export declare function validateRemoveFromCartOutput(value: unknown, path?: string): void;
export declare function validateReplaceCartItemsInput(value: unknown, path?: string): void;
export declare function validateReplaceCartItemsOutput(value: unknown, path?: string): void;
export declare function validateSearchProductsInput(value: unknown, path?: string): void;
export declare function validateSearchProductsOutput(value: unknown, path?: string): void;
export declare function validateSelectShippingOptionInput(value: unknown, path?: string): void;
export declare function validateSelectShippingOptionOutput(value: unknown, path?: string): void;
export declare function validateSetShippingAddressInput(value: unknown, path?: string): void;
export declare function validateSetShippingAddressOutput(value: unknown, path?: string): void;
export declare function validateShippingOption(value: unknown, path?: string): void;
export declare function validateUpdateCartItemInput(value: unknown, path?: string): void;
export declare function validateUpdateCartItemOutput(value: unknown, path?: string): void;
export declare function validateUpdateCheckoutInput(value: unknown, path?: string): void;
export declare function validateUpdateCheckoutOutput(value: unknown, path?: string): void;
export declare const OPERATIONS: Record<string, {
    contractVersion: string;
    method: string;
    path: string;
    sideEffect: string;
    idempotency: string;
    identity: string;
    validateInput: (value: unknown, path?: string) => void;
    validateOutput: (value: unknown, path?: string) => void;
}>;
