// Naming and field conventions shared by the binding planner and the
// per-language code generators. Everything here is a pure function of the
// locked registry operation name and contract, so generation is deterministic.
//
// The documented convention map (referenced from generated file headers):
//   - Registry operations and contract input fields are snake_case.
//   - Node/TS adapters pass arguments camelCased (product_id -> productId);
//     Python and Go keep the contract's snake_case names.
//   - Path parameters arrive in the SDK context under their contract names
//     (ctx.pathParams.cart_id / path_params["cart_id"] /
//     call.PathParams["cart_id"]) and are forwarded camelCased in Node calls.
//   - expected_revision is delivered by the SDK as ctx.expectedRevision when
//     the contract uses resource_revision concurrency; idempotency-required
//     operations also receive ctx.actionId as the idempotency key.

export const RESOURCE_BY_OPERATION = {
  add_to_cart: 'cart',
  update_cart_item: 'cart',
  remove_from_cart: 'cart',
  replace_cart_items: 'cart',
  create_cart: 'cart',
  get_cart: 'cart',
  cancel_cart: 'cart',
  apply_discount_code: 'discount',
  remove_discount_code: 'discount',
  create_checkout: 'checkout',
  get_checkout: 'checkout',
  update_checkout: 'checkout',
  complete_checkout: 'checkout',
  cancel_checkout: 'checkout',
  get_order: 'orders',
  search_products: 'catalog',
  get_product: 'catalog',
  get_shipping_options: 'shipping',
  select_shipping_option: 'shipping',
  set_shipping_address: 'shipping',
};

export function resourceOf(operation) {
  return RESOURCE_BY_OPERATION[operation] || operation.split('_').pop();
}

export function camelCase(name) {
  return String(name).replace(/_([a-z0-9])/g, (_, ch) => ch.toUpperCase());
}

export function pascalCase(name) {
  const camel = camelCase(name);
  return camel.charAt(0).toUpperCase() + camel.slice(1);
}

export function lowerFirst(name) {
  return name.charAt(0).toLowerCase() + name.slice(1);
}

// Contract route path parameters in declaration order:
// "/api/auteric/v1/carts/{cart_id}/items" -> ["cart_id"].
export function pathParams(contractPath) {
  return [...String(contractPath || '').matchAll(/\{([^}]+)\}/g)].map(match => match[1]);
}

// Original merchant route parameters in declaration order, Express ":id" or
// Starlette/Go "{id}" style: "/api/cart/:id/items" -> ["id"].
export function routeParams(routePath) {
  const found = [];
  for (const match of String(routePath || '').matchAll(/[:{]([A-Za-z_]\w*)\}?/g)) {
    if (!found.includes(match[1])) found.push(match[1]);
  }
  return found;
}

// Maps a JSON-schema property to a TypeScript type literal.
export function tsType(property) {
  if (!property || typeof property !== 'object') return 'unknown';
  if (property.$ref) return 'string'; // common/ids.json references are IDs
  if (property.type === 'integer' || property.type === 'number') return 'number';
  if (property.type === 'boolean') return 'boolean';
  if (property.type === 'string') return 'string';
  if (property.type === 'array') return 'unknown[]';
  return 'Record<string, unknown>';
}
