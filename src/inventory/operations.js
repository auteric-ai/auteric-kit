// Candidate vocabulary: the 20 registry operations from
// packages/commerce-contracts/registry/operations. The on-disk registry is the
// source of truth and is loaded when present; this table carries the matching
// signals so the kit also works installed standalone (npx). Signal shape:
//   methods  - HTTP methods compatible with the operation ('ANY' matches all)
//   groups   - token groups; every group must contribute at least one token
//   exclude  - tokens that disqualify the match (keeps create_cart off item routes)
//   side     - read|write per the registry contract
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const OPERATION_SIGNALS = [
  { operation: 'add_to_cart', methods: ['POST'], groups: [['cart', 'basket', 'bag'], ['item', 'items', 'line', 'lines', 'add', 'product']], side: 'write' },
  { operation: 'update_cart_item', methods: ['PATCH', 'PUT'], groups: [['cart', 'basket', 'bag'], ['item', 'items', 'line', 'lines']], exclude: ['replace'], side: 'write' },
  { operation: 'remove_from_cart', methods: ['DELETE', 'POST'], groups: [['cart', 'basket', 'bag'], ['item', 'items', 'line', 'lines', 'remove', 'delete']], exclude: ['add', 'create', 'update'], side: 'write' },
  { operation: 'replace_cart_items', methods: ['PUT'], groups: [['cart', 'basket', 'bag'], ['item', 'items', 'line', 'lines'], ['replace', 'set', 'bulk']], side: 'write' },
  { operation: 'create_cart', methods: ['POST'], groups: [['cart', 'basket', 'bag']], exclude: ['item', 'items', 'line', 'lines', 'cancel', 'discount', 'promo', 'coupon', 'shipping', 'checkout'], side: 'write' },
  { operation: 'get_cart', methods: ['GET'], groups: [['cart', 'basket', 'bag']], exclude: ['shipping', 'discount', 'items'], side: 'read' },
  { operation: 'cancel_cart', methods: ['POST', 'DELETE'], groups: [['cart', 'basket', 'bag'], ['cancel', 'abandon', 'void']], side: 'write' },
  { operation: 'apply_discount_code', methods: ['POST'], groups: [['discount', 'promo', 'promotion', 'coupon', 'voucher'], ['apply', 'add', 'code', 'codes', 'redeem']], side: 'write' },
  { operation: 'remove_discount_code', methods: ['DELETE', 'POST'], groups: [['discount', 'promo', 'promotion', 'coupon', 'voucher'], ['remove', 'delete', 'clear']], side: 'write' },
  { operation: 'create_checkout', methods: ['POST'], groups: [['checkout']], exclude: ['complete', 'cancel', 'shipping', 'address', 'confirm', 'submit'], side: 'write' },
  { operation: 'get_checkout', methods: ['GET'], groups: [['checkout']], exclude: ['shipping'], side: 'read' },
  { operation: 'update_checkout', methods: ['PATCH', 'PUT'], groups: [['checkout']], exclude: ['shipping', 'address'], side: 'write' },
  { operation: 'complete_checkout', methods: ['POST'], groups: [['checkout', 'order', 'payment'], ['complete', 'confirm', 'submit', 'place', 'finish', 'capture']], side: 'write' },
  { operation: 'cancel_checkout', methods: ['POST', 'DELETE'], groups: [['checkout'], ['cancel', 'void', 'abort']], side: 'write' },
  { operation: 'get_order', methods: ['GET'], groups: [['order', 'orders']], exclude: ['shipping'], side: 'read' },
  { operation: 'search_products', methods: ['GET', 'POST'], groups: [['product', 'products', 'catalog', 'item', 'items'], ['search', 'query', 'find', 'list', 'browse']], side: 'read' },
  { operation: 'get_product', methods: ['GET'], groups: [['product', 'products', 'item', 'items', 'sku', 'variant']], exclude: ['search', 'cart', 'list'], side: 'read' },
  { operation: 'get_shipping_options', methods: ['GET'], groups: [['shipping', 'delivery', 'shipment'], ['option', 'options', 'rate', 'rates', 'method', 'methods', 'quote', 'quotes']], side: 'read' },
  { operation: 'select_shipping_option', methods: ['PUT', 'POST'], groups: [['shipping', 'delivery', 'shipment'], ['option', 'options', 'rate', 'rates', 'method', 'methods'], ['select', 'choose', 'set', 'pick']], side: 'write' },
  { operation: 'set_shipping_address', methods: ['PUT', 'POST', 'PATCH'], groups: [['shipping', 'delivery', 'shipment', 'checkout'], ['address']], side: 'write' },
];

// Locates the checked-in contract registry first, then the immutable registry
// bundled with the Kit. The latter keeps npx/plugin installs standalone: a
// merchant must not need the private monorepo to prepare a Native runtime.
export function loadOperationsRegistry(overrideDir) {
  const readFrom = dir => {
    const operations = {};
    for (const name of readdirSync(dir).sort()) {
      if (!name.endsWith('.json')) continue;
      try {
        const record = JSON.parse(readFileSync(join(dir, name), 'utf8'));
        if (record.operation) operations[record.operation] = record;
      } catch { /* unreadable registry record is ignored */ }
    }
    return Object.keys(operations).length ? { path: dir, operations } : null;
  };
  if (overrideDir) return existsSync(overrideDir) ? readFrom(overrideDir) : null;
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 8; depth++) {
    const candidate = join(dir, 'packages', 'commerce-contracts', 'registry', 'operations');
    if (existsSync(candidate)) return readFrom(candidate);
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  const bundled = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'runtime', 'contracts', 'registry', 'operations');
  if (existsSync(bundled)) return readFrom(bundled);
  return null;
}
