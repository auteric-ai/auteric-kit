// Acceptance scenario planning. Builds the per-operation scenario list from
// the locked registry record (test_scenarios, side_effect, idempotency,
// ownership) plus the conformance vector inputs. The plan is data-only; the
// runner maps each kind to an executor. Scenarios the runner cannot set up
// honestly are reported pending with a reason — never skipped silently.
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { resourceOf } from '../binding/naming.js';

export function loadVectors(vectorsDir, operation) {
  const result = { happy: null, negatives: [] };
  if (!vectorsDir || !existsSync(vectorsDir)) return result;
  for (const name of readdirSync(vectorsDir).sort()) {
    if (!name.startsWith(`${operation}.`) || !name.endsWith('.json')) continue;
    try {
      const vector = JSON.parse(readFileSync(join(vectorsDir, name), 'utf8'));
      if (name.includes('.negative.')) result.negatives.push({ name: vector.name || name, input: vector.input, error_category: vector.error_category });
      else if (name.endsWith('.happy.json')) result.happy = vector.input ?? {};
    } catch { /* unreadable vector is ignored */ }
  }
  return result;
}

const BODILESS = new Set(['GET', 'HEAD', 'DELETE']);

function hasPathParams(record) {
  return /\{[a-z_][a-z0-9_]*\}/.test(record.merchant_http?.path || '');
}

// The invalid-input probe: a conformance negative when the operation takes a
// JSON body; an unknown query field for bodiless operations (query values are
// coerced by the SDK, so type-corruption negatives only exist over a body).
function invalidInputProbe(operation, record, vectors) {
  const method = record.merchant_http?.method || 'GET';
  if (!BODILESS.has(method)) {
    const negative = vectors.negatives.find(item => item.input && typeof item.input === 'object');
    if (negative) return { body: negative.input, source: `conformance vector ${negative.name}` };
    return null;
  }
  return { query: 'unexpected_field=1', source: 'additionalProperties probe (unknown query field)' };
}

export function buildScenarioPlan(operation, record, context = {}) {
  const vectors = loadVectors(context.vectorsDir, operation);
  const resource = resourceOf(operation);
  const bound = context.boundOperations || new Set();
  const method = record.merchant_http?.method;
  const sideEffect = record.side_effect;
  const idempotent = record.idempotency === 'required';
  const owned = Boolean(record.ownership?.rule);
  const scenarios = [];
  const pending = (name, reason) => scenarios.push({ name, kind: 'pending', reason });

  if (!method || !record.merchant_http?.path) {
    pending('happy-path', `registry record for ${operation} has no merchant_http route`);
    return scenarios;
  }

  const happyInput = vectors.happy && typeof vectors.happy === 'object'
    ? Object.fromEntries(Object.entries(vectors.happy).filter(([key]) => key !== 'expected_revision'))
    : null;

  if (resource === 'cart') {
    // Cart lifecycle scenarios assert merchant state through get_cart, so an
    // adapter that returns constant JSON without mutating anything fails.
    const stateProbe = bound.has('get_cart') ? 'get_cart' : null;
    const setup = bound.has('add_to_cart') || operation === 'add_to_cart';
    if (operation === 'add_to_cart') {
      if (happyInput) scenarios.push({ name: 'happy-path', kind: 'cart-add-lifecycle', input: happyInput, stateProbe });
      else pending('happy-path', 'no conformance happy input for add_to_cart');
    } else if (operation === 'get_cart') {
      if (setup) scenarios.push({ name: 'happy-path', kind: 'cart-read-consistency', stateProbe });
      else pending('happy-path', 'setup requires add_to_cart, which is not bound');
    } else if (operation === 'remove_from_cart') {
      if (setup) scenarios.push({ name: 'happy-path', kind: 'cart-remove-lifecycle', stateProbe });
      else pending('happy-path', 'setup requires add_to_cart, which is not bound');
    } else {
      pending('happy-path', `no lifecycle executor for cart operation ${operation} yet`);
    }
    if (operation === 'get_cart') scenarios.push({ name: 'unknown-resource', kind: 'cart-unknown-resource' });
    if (operation === 'remove_from_cart') {
      if (setup) scenarios.push({ name: 'unknown-resource', kind: 'cart-unknown-line' });
      else pending('unknown-resource', 'setup requires add_to_cart, which is not bound');
    }
  } else if (resource === 'catalog') {
    if (operation === 'search_products' && happyInput) {
      scenarios.push({ name: 'happy-path', kind: 'catalog-search', input: happyInput });
    } else if (operation === 'get_product') {
      scenarios.push({ name: 'happy-path', kind: 'catalog-get-product' });
      scenarios.push({ name: 'unknown-resource', kind: 'catalog-unknown-product' });
    } else {
      pending('happy-path', `no lifecycle executor for catalog operation ${operation} yet`);
    }
  } else if (resource === 'checkout') {
    const setup = bound.has('add_to_cart');
    if (operation === 'create_checkout') {
      if (setup) scenarios.push({ name: 'happy-path', kind: 'checkout-create-lifecycle' });
      else pending('happy-path', 'setup requires add_to_cart, which is not bound');
    } else if (operation === 'get_checkout') {
      if (setup && bound.has('create_checkout')) scenarios.push({ name: 'happy-path', kind: 'checkout-read-lifecycle' });
      else pending('happy-path', 'setup requires add_to_cart and create_checkout, which are not both bound');
      if (setup && bound.has('create_checkout')) scenarios.push({ name: 'unknown-resource', kind: 'checkout-unknown-resource' });
    } else {
      pending('happy-path', `no lifecycle executor for checkout operation ${operation} yet`);
    }
  } else if (!hasPathParams(record) && happyInput) {
    // Resource-free reads (for example search_products) get a smoke scenario.
    scenarios.push({ name: 'happy-path', kind: 'smoke', input: happyInput });
  } else {
    pending('happy-path', hasPathParams(record)
      ? `cannot create a ${resource} resource through the bound operations; provide a state-probe hook or bind its creating operation`
      : `no conformance happy input for ${operation}`);
  }

  const probe = invalidInputProbe(operation, record, vectors);
  if (probe) scenarios.push({ name: 'invalid-input', kind: 'invalid-input', ...probe });
  else pending('invalid-input', `no negative conformance vector for ${operation}`);

  if (owned && record.ownership?.rule !== 'public_catalog_read' && hasPathParams(record)) {
    if (resource === 'cart' && (bound.has('add_to_cart') || operation === 'add_to_cart')) {
      scenarios.push({ name: 'wrong-owner-denial', kind: 'cart-wrong-owner', operation, method });
    } else if (resource === 'checkout' && bound.has('add_to_cart') && bound.has('create_checkout')) {
      scenarios.push({ name: 'wrong-owner-denial', kind: 'checkout-wrong-owner', operation, method });
    } else {
      pending('wrong-owner-denial', `cannot set up a principal-owned ${resource} with the bound operations`);
    }
  }

  if (sideEffect === 'write' && idempotent) {
    if (operation === 'add_to_cart' && happyInput) scenarios.push({ name: 'idempotent-retry', kind: 'cart-add-idempotency', input: happyInput, stateProbe: bound.has('get_cart') ? 'get_cart' : null });
    else if (operation === 'remove_from_cart' && bound.has('add_to_cart')) scenarios.push({ name: 'idempotent-retry', kind: 'cart-remove-idempotency', stateProbe: bound.has('get_cart') ? 'get_cart' : null });
    else if (operation === 'create_checkout' && bound.has('add_to_cart')) scenarios.push({ name: 'idempotent-retry', kind: 'checkout-create-idempotency' });
    else if (operation !== 'add_to_cart' && operation !== 'remove_from_cart') pending('idempotent-retry', `no idempotency executor for ${operation} yet`);
    if (operation === 'add_to_cart' && happyInput) scenarios.push({ name: 'idempotency-conflict', kind: 'cart-add-conflict', input: happyInput });
    if (operation === 'remove_from_cart' && bound.has('add_to_cart')) scenarios.push({ name: 'idempotency-conflict', kind: 'cart-remove-conflict' });
  }

  return scenarios;
}
