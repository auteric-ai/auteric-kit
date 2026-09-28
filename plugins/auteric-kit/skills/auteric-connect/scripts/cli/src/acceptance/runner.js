// Contract acceptance runner (P1-13). Independent of the binding generator:
// it copies the merchant tree to a throwaway workspace, installs the pinned
// merchant SDK and framework from local sources (offline), boots the generated
// composition root under a dev installation with an ephemeral Ed25519 gateway
// key, and executes the locked-contract scenarios over real HTTP. Verdicts are
// based on merchant state, not on status codes alone: an adapter that returns
// constant JSON without mutating anything fails the lifecycle scenarios.
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { loadOperationsRegistry } from '../inventory/operations.js';
import { readManifest, MANIFEST_PATH } from '../binding/manifest.js';
import { validateInstallation } from '../binding/validate.js';
import { sha256Hex } from '../binding/util.js';
import { nullProgress } from '../progress.js';
import { generateGatewayKeys, makeInstallation, makeTrust, DevSigner, installationBindingDigest } from './signer.js';
import { validateOperationOutput, validateErrorEnvelope } from './contracts.js';
import { buildScenarioPlan } from './scenarios.js';
import { buildReport, writeReport, operationRecord, REPORT_PATH } from './report.js';
import { HARNESS_DIR, SERVER_MJS, TS_RESOLVE_HOOK_MJS, REGISTER_HOOK_MJS } from './harness.js';

const SUB_A = 'buyer_pairwise_a01';
const SUB_B = 'buyer_pairwise_b01';
const PRINCIPALS = { [SUB_A]: 'buyer_acceptance001', [SUB_B]: 'buyer_acceptance002' };
const READY_TIMEOUT_MS = 20000;

class ScenarioFailure extends Error {}

function fail(message) {
  throw new ScenarioFailure(message);
}

// --- offline dependency installation -------------------------------------

function findUp(relativePath) {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 10; depth++) {
    if (existsSync(join(dir, relativePath))) return join(dir, dirname(relativePath));
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function copyPackageWithDeps(modulesDir, name, destModulesDir, seen = new Set()) {
  if (seen.has(name)) return;
  seen.add(name);
  const source = join(modulesDir, name);
  if (!existsSync(join(source, 'package.json'))) throw Error(`offline install: package ${name} not found under ${modulesDir}`);
  cpSync(source, join(destModulesDir, name), { recursive: true, dereference: true });
  const pkg = JSON.parse(readFileSync(join(source, 'package.json'), 'utf8'));
  for (const dependency of Object.keys(pkg.dependencies || {})) copyPackageWithDeps(modulesDir, dependency, destModulesDir, seen);
}

// Installs what the generated composition root imports: the pinned merchant
// SDK and the web framework. Everything comes from local checkouts — no npm,
// no network — so acceptance works in offline sandboxes.
export function installNodeRuntime(workRoot, options = {}) {
  const modulesDir = join(workRoot, 'node_modules');
  mkdirSync(join(modulesDir, '@auteric'), { recursive: true });
  const sdkDir = options.merchantNodeDir || findUp(join('packages', 'merchant-node', 'package.json'));
  if (!sdkDir) throw Error('offline install: packages/merchant-node not found; pass merchantNodeDir');
  if (!existsSync(join(sdkDir, 'dist', 'index.js'))) throw Error(`offline install: ${sdkDir} has no built dist; run its build first`);
  cpSync(sdkDir, join(modulesDir, '@auteric', 'merchant-node'), {
    recursive: true,
    filter: source => !/node_modules|dist-test/.test(relative(sdkDir, source)),
  });
  const expressModules = options.expressModulesDir
    || (existsSync(join(sdkDir, 'node_modules', 'express', 'package.json')) ? join(sdkDir, 'node_modules') : null)
    || (() => { const found = findUp(join('node_modules', 'express', 'package.json')); return found ? dirname(found) : null; })();
  if (!expressModules) throw Error('offline install: express package not found; pass expressModulesDir');
  copyPackageWithDeps(expressModules, 'express', modulesDir);
}

// --- workspace ------------------------------------------------------------

// The runner works on a copy of the merchant tree; the source fixture is
// never mutated. The only artifact written back is the redacted report.
function copyWorkspace(root) {
  const workspace = mkdtempSync(join(tmpdir(), 'auteric-acceptance-'));
  const workRoot = join(workspace, 'merchant');
  cpSync(root, workRoot, {
    recursive: true,
    filter: source => {
      const rel = relative(root, source).split(sep).join('/');
      if (rel === '') return true;
      const top = rel.split('/')[0];
      if (top === 'node_modules' || top === '.git') return false;
      if (rel === HARNESS_DIR || rel.startsWith(HARNESS_DIR + '/')) return false;
      return true;
    },
  });
  return { workspace, workRoot };
}

function writeHarness(workRoot, manifest, devRuntime) {
  const dir = join(workRoot, HARNESS_DIR);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'server.mjs'), SERVER_MJS);
  writeFileSync(join(dir, 'ts-resolve-hook.mjs'), TS_RESOLVE_HOOK_MJS);
  writeFileSync(join(dir, 'register-ts.mjs'), REGISTER_HOOK_MJS);
  writeFileSync(join(dir, 'dev-runtime.json'), JSON.stringify({
    composition_root: manifest.composition_root,
    port: devRuntime.port,
    hostname: devRuntime.hostname,
    installation: devRuntime.installation,
    trust: devRuntime.trust,
    principals: devRuntime.principals,
  }, null, 2) + '\n', { mode: 0o600 });
}

// --- dev server lifecycle -------------------------------------------------

function startServer(workRoot, { progress, reportPath, nodeArgs = [] }) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(process.execPath, [...nodeArgs, '--import', `./${HARNESS_DIR}/register-ts.mjs`, `${HARNESS_DIR}/server.mjs`], {
      cwd: workRoot,
      env: { ...process.env, AUTERIC_ACCEPTANCE_ROOT: workRoot },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill('SIGKILL');
      progress?.timeout({
        phase: 'Acceptance',
        lastAction: `start the merchant dev server (node ${HARNESS_DIR}/server.mjs)`,
        reportPaths: [reportPath],
        resumeHint: 'start the composition root manually to see the boot error, then re-run `auteric verify`',
      });
      reject(Error(`merchant dev server did not become ready within ${READY_TIMEOUT_MS / 1000}s: ${stderr.trim().slice(-400) || 'no output'}`));
    }, READY_TIMEOUT_MS);
    child.stdout.on('data', chunk => {
      stdout += chunk;
      const match = stdout.match(/auteric-acceptance-ready (\{[^\n]*\})/);
      if (match && !settled) {
        settled = true;
        clearTimeout(timer);
        const ready = JSON.parse(match[1]);
        resolvePromise({
          port: ready.port,
          stop: () => new Promise(done => {
            const grace = setTimeout(() => { child.kill('SIGKILL'); }, 5000);
            child.once('exit', () => { clearTimeout(grace); done(); });
            child.kill('SIGTERM');
          }),
        });
      }
    });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('exit', code => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(Error(`merchant dev server exited before readiness (code ${code}): ${stderr.trim().slice(-400) || 'no error output'}`));
      }
    });
  });
}

// --- HTTP execution -------------------------------------------------------

async function callOperation(base, signer, record, { pathParams = {}, query = '', body = null, sub, actionId }) {
  const method = record.merchant_http.method;
  const path = record.merchant_http.path.replace(/\{([^}]+)\}/g, (_, name) => {
    if (!(name in pathParams)) throw Error(`missing path parameter ${name}`);
    return encodeURIComponent(pathParams[name]);
  });
  const bodyBytes = body === null ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body), 'utf8');
  const { token, actionId: usedAction } = signer.signToken({
    method, rawPath: path, rawQuery: query, body: bodyBytes,
    operation: record.operation, sub, actionId, contractVersion: record.contract_version,
  });
  let response;
  try {
    response = await fetch(`${base}${path}${query ? `?${query}` : ''}`, {
      method,
      headers: {
        authorization: `Bearer ${token}`,
        ...(bodyBytes.length ? { 'content-type': 'application/json' } : {}),
      },
      body: bodyBytes.length ? bodyBytes : undefined,
      signal: AbortSignal.timeout(10000),
    });
  } catch (error) {
    fail(`request ${method} ${path} failed: ${error.message}`);
  }
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* non-JSON handled below */ }
  return { status: response.status, json, text, actionId: usedAction };
}

function assertCartConsistent(cart, label) {
  for (const line of cart.line_items || []) {
    assert.equal(line.total_price.amount_minor, line.unit_price.amount_minor * line.quantity, `${label}: line ${line.line_id} total != unit × quantity`);
  }
  const subtotal = (cart.line_items || []).reduce((sum, line) => sum + line.total_price.amount_minor, 0);
  assert.equal(cart.subtotal.amount_minor, subtotal, `${label}: subtotal is not the sum of line totals`);
  assert.equal(cart.total.amount_minor, subtotal - cart.discount_total.amount_minor, `${label}: total != subtotal - discounts`);
}

function expectSuccess(response, operation, label) {
  if (response.status !== 200) {
    const code = response.json?.error?.code;
    fail(`${label}: expected 200, got ${response.status}${code ? ` ${code}` : ''}`);
  }
  if (!response.json || typeof response.json !== 'object') fail(`${label}: response is not a JSON object`);
  try {
    validateOperationOutput(operation, response.json);
  } catch (error) {
    fail(`${label}: output breaks the locked ${operation} schema (${error.message})`);
  }
}

function expectWireError(response, statuses, codes, label) {
  if (!statuses.includes(response.status)) fail(`${label}: expected HTTP ${statuses.join(' or ')}, got ${response.status}`);
  const code = response.json?.error?.code;
  if (!codes.includes(code)) fail(`${label}: expected error code ${codes.join(' or ')}, got ${JSON.stringify(code)}`);
  let note = '';
  try {
    const notes = validateErrorEnvelope(response.json);
    if (notes.length) note = ` (${notes.join('; ')})`;
  } catch (error) {
    fail(`${label}: error envelope breaks the locked schema (${error.message})`);
  }
  return `${response.status} ${code}${note}`;
}

function cartId() {
  return `cart_${randomBytes(8).toString('hex')}`;
}

function queryForInput(input) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(input || {})) {
    if (value !== undefined && value !== null) params.set(key, String(value));
  }
  return params.toString();
}

// --- scenario executors ----------------------------------------------------

function makeExecutors(ctx) {
  const { call, records } = ctx;

  async function setupCart(sub, { quantity = 2, productId = 'prod_acceptance01' } = {}) {
    const id = cartId();
    const response = await call('add_to_cart', { pathParams: { cart_id: id }, body: { product_id: productId, quantity }, sub });
    expectSuccess(response, 'add_to_cart', 'scenario setup (add_to_cart)');
    return { cartId: id, cart: response.json, lineId: response.json.line_items[0]?.line_id, actionId: response.actionId };
  }

  async function stateRead(id, label) {
    if (!records.get_cart) return null;
    const response = await call('get_cart', { pathParams: { cart_id: id }, sub: SUB_A });
    expectSuccess(response, 'get_cart', label);
    return response.json;
  }

  async function setupCheckout(sub = SUB_A) {
    const cart = await setupCart(sub);
    const response = await call('create_checkout', {
      body: { cart_id: cart.cartId, expected_cart_revision: cart.cart.resource_revision },
      sub,
    });
    expectSuccess(response, 'create_checkout', 'scenario setup (create_checkout)');
    return { ...cart, checkout: response.json, checkoutId: response.json.checkout_id, actionId: response.actionId };
  }

  return {
    'cart-add-lifecycle': async scenario => {
      const id = cartId();
      const response = await call('add_to_cart', { pathParams: { cart_id: id }, body: scenario.input, sub: SUB_A });
      expectSuccess(response, 'add_to_cart', 'happy-path');
      const cart = response.json;
      const line = cart.line_items.find(item => item.product_id === scenario.input.product_id);
      if (!line) fail(`happy-path: add response has no line for ${scenario.input.product_id}`);
      assert.equal(line.quantity, scenario.input.quantity, 'happy-path: added line quantity differs from the request');
      try { assertCartConsistent(cart, 'add response'); } catch (error) { fail(error.message); }
      let stateDetail = 'state probe unavailable (get_cart not bound)';
      if (scenario.stateProbe) {
        const state = await stateRead(id, 'state check after add');
        const stateLine = state.line_items.find(item => item.product_id === scenario.input.product_id);
        if (!stateLine || stateLine.quantity !== scenario.input.quantity) {
          fail('state check: follow-up get_cart does not show the added line — the adapter response is not backed by merchant state');
        }
        assert.equal(state.resource_revision, cart.resource_revision, 'state check: get_cart revision differs from the add response');
        assert.deepEqual(state.subtotal, cart.subtotal, 'state check: get_cart totals differ from the add response');
        stateDetail = `follow-up get_cart shows the line at revision ${state.resource_revision}`;
      }
      return `added ${scenario.input.quantity} × ${scenario.input.product_id}; ${stateDetail}; totals internally consistent`;
    },

    'cart-read-consistency': async () => {
      const setup = await setupCart(SUB_A);
      const first = await call('get_cart', { pathParams: { cart_id: setup.cartId }, sub: SUB_A });
      expectSuccess(first, 'get_cart', 'happy-path');
      if (!first.json.line_items.some(item => item.line_id === setup.lineId)) fail('happy-path: owner read does not show the line added during setup');
      const second = await call('get_cart', { pathParams: { cart_id: setup.cartId }, sub: SUB_A });
      expectSuccess(second, 'get_cart', 'retry stability');
      assert.deepEqual(second.json, first.json, 'retry stability: repeated read changed the cart');
      try { assertCartConsistent(first.json, 'get_cart response'); } catch (error) { fail(error.message); }
      return `owner read shows line ${setup.lineId}; repeated read stable at revision ${first.json.resource_revision}`;
    },

    'cart-remove-lifecycle': async () => {
      const setup = await setupCart(SUB_A);
      const response = await call('remove_from_cart', { pathParams: { cart_id: setup.cartId, line_id: setup.lineId }, sub: SUB_A });
      expectSuccess(response, 'remove_from_cart', 'happy-path');
      if (response.json.line_items.some(item => item.line_id === setup.lineId)) fail('happy-path: removed line still present in the response');
      if (!(response.json.resource_revision > setup.cart.resource_revision)) fail('happy-path: resource_revision did not increase after removal');
      try { assertCartConsistent(response.json, 'remove response'); } catch (error) { fail(error.message); }
      let stateDetail = 'state probe unavailable (get_cart not bound)';
      const state = await stateRead(setup.cartId, 'state check after remove');
      if (state) {
        if (state.line_items.some(item => item.line_id === setup.lineId)) fail('state check: removed line still present in merchant state');
        assert.equal(state.resource_revision, response.json.resource_revision, 'state check: get_cart revision differs from the remove response');
        stateDetail = `follow-up get_cart confirms removal at revision ${state.resource_revision} with totals recomputed`;
      }
      return `removed line ${setup.lineId}; ${stateDetail}`;
    },

    'cart-unknown-resource': async () => {
      const response = await call('get_cart', { pathParams: { cart_id: cartId() }, sub: SUB_A });
      return `unknown cart_id rejected: ${expectWireError(response, [404], ['RESOURCE_NOT_FOUND'], 'unknown-resource')}`;
    },

    'cart-unknown-line': async () => {
      const setup = await setupCart(SUB_A);
      const response = await call('remove_from_cart', { pathParams: { cart_id: setup.cartId, line_id: 'line_000000000000' }, sub: SUB_A });
      return `unknown line_id rejected: ${expectWireError(response, [404], ['RESOURCE_NOT_FOUND'], 'unknown-resource')}`;
    },

    'cart-wrong-owner': async scenario => {
      const setup = await setupCart(SUB_A);
      const params = { pathParams: { cart_id: setup.cartId }, sub: SUB_B };
      if (scenario.operation === 'remove_from_cart') params.pathParams.line_id = setup.lineId;
      if (scenario.operation === 'add_to_cart') params.body = { product_id: 'prod_acceptance02', quantity: 1 };
      const response = await call(scenario.operation, params);
      const outcome = expectWireError(response, [403, 404], ['FORBIDDEN', 'RESOURCE_NOT_FOUND'], 'wrong-owner-denial');
      return `principal B calling ${scenario.operation} on principal A's cart received ${outcome}`;
    },

    'invalid-input': async scenario => {
      let pathParams = { cart_id: cartId() };
      if (ctx.operation === 'remove_from_cart') {
        const setup = await setupCart(SUB_A);
        pathParams = { cart_id: setup.cartId, line_id: setup.lineId };
      } else if (ctx.operation === 'get_product') {
        pathParams = { product_id: 'prod_acceptance01' };
      } else if (ctx.operation === 'get_checkout') {
        const setup = await setupCheckout(SUB_A);
        pathParams = { checkout_id: setup.checkoutId };
      }
      const response = await call(ctx.operation, {
        pathParams,
        query: scenario.query || '',
        body: scenario.body ?? null,
        sub: SUB_A,
      });
      const outcome = expectWireError(response, [400], ['INVALID_INPUT'], 'invalid-input');
      return `schema violation rejected: ${outcome} [${scenario.source}]`;
    },

    'cart-add-idempotency': async scenario => {
      const id = cartId();
      const first = await call('add_to_cart', { pathParams: { cart_id: id }, body: scenario.input, sub: SUB_A, actionId: undefined });
      expectSuccess(first, 'add_to_cart', 'idempotent-retry first attempt');
      const second = await call('add_to_cart', { pathParams: { cart_id: id }, body: scenario.input, sub: SUB_A, actionId: first.actionId });
      expectSuccess(second, 'add_to_cart', 'idempotent-retry duplicate attempt');
      assert.deepEqual(second.json, first.json, 'idempotent-retry: duplicate action_id did not return the stored result');
      let stateDetail = 'state probe unavailable (get_cart not bound)';
      if (scenario.stateProbe) {
        const state = await stateRead(id, 'idempotency state check');
        const line = state.line_items.find(item => item.product_id === scenario.input.product_id);
        if (!line) fail('idempotency state check: line missing after duplicate request');
        assert.equal(line.quantity, scenario.input.quantity, 'idempotency state check: duplicate action_id applied the business effect twice');
        stateDetail = `merchant state holds exactly one effect (quantity ${line.quantity})`;
      }
      return `duplicate action_id returned the stored result; ${stateDetail}`;
    },

    'cart-add-conflict': async scenario => {
      const id = cartId();
      const first = await call('add_to_cart', { pathParams: { cart_id: id }, body: scenario.input, sub: SUB_A });
      expectSuccess(first, 'add_to_cart', 'idempotency-conflict setup');
      const changed = { ...scenario.input, quantity: scenario.input.quantity + 1 };
      const response = await call('add_to_cart', { pathParams: { cart_id: id }, body: changed, sub: SUB_A, actionId: first.actionId });
      return `same action_id with a changed payload rejected: ${expectWireError(response, [409], ['IDEMPOTENCY_CONFLICT'], 'idempotency-conflict')}`;
    },

    'cart-remove-idempotency': async scenario => {
      const setup = await setupCart(SUB_A);
      const first = await call('remove_from_cart', { pathParams: { cart_id: setup.cartId, line_id: setup.lineId }, sub: SUB_A });
      expectSuccess(first, 'remove_from_cart', 'idempotent-retry first attempt');
      const second = await call('remove_from_cart', { pathParams: { cart_id: setup.cartId, line_id: setup.lineId }, sub: SUB_A, actionId: first.actionId });
      expectSuccess(second, 'remove_from_cart', 'idempotent-retry duplicate attempt');
      assert.deepEqual(second.json, first.json, 'idempotent-retry: duplicate action_id did not return the stored result');
      let stateDetail = 'state probe unavailable (get_cart not bound)';
      if (scenario.stateProbe) {
        const state = await stateRead(setup.cartId, 'idempotency state check');
        if (state.line_items.some(item => item.line_id === setup.lineId)) fail('idempotency state check: removed line reappeared after the duplicate request');
        assert.equal(state.resource_revision, first.json.resource_revision, 'idempotency state check: duplicate request bumped the revision');
        stateDetail = `merchant state unchanged by the duplicate (revision ${state.resource_revision})`;
      }
      return `duplicate action_id returned the stored result; ${stateDetail}`;
    },

    'cart-remove-conflict': async () => {
      const id = cartId();
      const first = await call('add_to_cart', { pathParams: { cart_id: id }, body: { product_id: 'prod_acceptance01', quantity: 1 }, sub: SUB_A });
      expectSuccess(first, 'add_to_cart', 'idempotency-conflict setup line 1');
      const second = await call('add_to_cart', { pathParams: { cart_id: id }, body: { product_id: 'prod_acceptance02', quantity: 1 }, sub: SUB_A });
      expectSuccess(second, 'add_to_cart', 'idempotency-conflict setup line 2');
      const [line1, line2] = second.json.line_items;
      const removed = await call('remove_from_cart', { pathParams: { cart_id: id, line_id: line1.line_id }, sub: SUB_A });
      expectSuccess(removed, 'remove_from_cart', 'idempotency-conflict first action');
      const response = await call('remove_from_cart', { pathParams: { cart_id: id, line_id: line2.line_id }, sub: SUB_A, actionId: removed.actionId });
      return `same action_id against a different line rejected: ${expectWireError(response, [409], ['IDEMPOTENCY_CONFLICT'], 'idempotency-conflict')}`;
    },

    'catalog-search': async scenario => {
      const response = await call('search_products', { query: queryForInput(scenario.input), sub: SUB_A });
      expectSuccess(response, 'search_products', 'happy-path');
      if (!response.json.results.some(product => product.product_id === 'prod_acceptance01')) {
        fail('happy-path: search did not return the known acceptance product');
      }
      return `query returned ${response.json.results.length} result(s), including the known acceptance product`;
    },

    'catalog-get-product': async () => {
      const response = await call('get_product', { pathParams: { product_id: 'prod_acceptance01' }, sub: SUB_A });
      expectSuccess(response, 'get_product', 'happy-path');
      if (response.json.product_id !== 'prod_acceptance01') fail('happy-path: product lookup returned the wrong product');
      return 'known acceptance product was returned with a contract-valid variant';
    },

    'catalog-unknown-product': async () => {
      const response = await call('get_product', { pathParams: { product_id: 'prod_unknown999' }, sub: SUB_A });
      return `unknown product rejected: ${expectWireError(response, [404], ['RESOURCE_NOT_FOUND'], 'unknown-resource')}`;
    },

    'checkout-create-lifecycle': async () => {
      const setup = await setupCheckout();
      const checkout = setup.checkout;
      if (checkout.cart_id !== setup.cartId) fail('happy-path: checkout was not created from the setup cart');
      if (checkout.payment.status !== 'not_started') fail('happy-path: checkout must be a handoff, not a completed payment');
      if (checkout.status === 'completed') fail('happy-path: acceptance must not complete a payment');
      return `checkout ${setup.checkoutId} created from owned cart ${setup.cartId}; payment remains ${checkout.payment.status}`;
    },

    'checkout-read-lifecycle': async () => {
      const setup = await setupCheckout();
      const response = await call('get_checkout', { pathParams: { checkout_id: setup.checkoutId }, sub: SUB_A });
      expectSuccess(response, 'get_checkout', 'happy-path');
      assert.deepEqual(response.json, setup.checkout, 'happy-path: checkout read differs from the created checkout');
      return `owner read checkout ${setup.checkoutId}; payment remains ${response.json.payment.status}`;
    },

    'checkout-unknown-resource': async () => {
      const response = await call('get_checkout', { pathParams: { checkout_id: 'chk_unknown999' }, sub: SUB_A });
      return `unknown checkout rejected: ${expectWireError(response, [404], ['RESOURCE_NOT_FOUND'], 'unknown-resource')}`;
    },

    'checkout-wrong-owner': async scenario => {
      const setup = await setupCheckout(SUB_A);
      if (scenario.operation === 'create_checkout') {
        const response = await call('create_checkout', {
          body: { cart_id: setup.cartId, expected_cart_revision: setup.cart.resource_revision }, sub: SUB_B,
        });
        return `principal B creating from principal A's cart received ${expectWireError(response, [403, 404], ['FORBIDDEN', 'RESOURCE_NOT_FOUND'], 'wrong-owner-denial')}`;
      }
      const response = await call('get_checkout', { pathParams: { checkout_id: setup.checkoutId }, sub: SUB_B });
      return `principal B reading principal A's checkout received ${expectWireError(response, [403, 404], ['FORBIDDEN', 'RESOURCE_NOT_FOUND'], 'wrong-owner-denial')}`;
    },

    'checkout-create-idempotency': async () => {
      const cart = await setupCart(SUB_A);
      const input = { cart_id: cart.cartId, expected_cart_revision: cart.cart.resource_revision };
      const first = await call('create_checkout', { body: input, sub: SUB_A });
      expectSuccess(first, 'create_checkout', 'idempotent-retry first attempt');
      const second = await call('create_checkout', { body: input, sub: SUB_A, actionId: first.actionId });
      expectSuccess(second, 'create_checkout', 'idempotent-retry duplicate attempt');
      assert.deepEqual(second.json, first.json, 'idempotent-retry: duplicate checkout action did not return the stored result');
      return `duplicate action_id returned checkout ${first.json.checkout_id} without initiating payment`;
    },

    smoke: async scenario => {
      const method = records[ctx.operation]?.merchant_http?.method;
      const response = await call(ctx.operation, method === 'GET'
        ? { query: queryForInput(scenario.input), sub: SUB_A }
        : { body: scenario.input ?? null, sub: SUB_A });
      expectSuccess(response, ctx.operation, 'happy-path');
      return `executed ${ctx.operation} with the conformance happy input; output validates against the locked schema`;
    },
  };
}

// --- main entry -------------------------------------------------------------

export async function runAcceptance(root, options = {}) {
  root = resolve(root);
  const progress = options.progress || nullProgress;
  const manifest = readManifest(root);
  if (!manifest) throw Error(`no installation manifest at ${MANIFEST_PATH}; run \`auteric bind\` first`);
  const validation = validateInstallation(root, { registryDir: options.registryDir });
  const operationNames = Object.keys(manifest.operations || {}).sort();
  const registry = loadOperationsRegistry(options.registryDir || (existsSync(manifest.registry?.operations_dir || '') ? manifest.registry.operations_dir : undefined));
  const vectorsDir = options.vectorsDir || (registry ? join(registry.path, '..', '..', 'vectors', 'conformance') : null);
  const reportPath = join(root, REPORT_PATH);

  const finish = operations => {
    const report = buildReport({ manifest, validation, operations });
    const path = writeReport(root, report);
    return { report, path, summary: report.summary };
  };

  progress.phase('Acceptance', 'started', { detail: `Acceptance: static validation, then dev-mode execution of ${operationNames.length} operation(s)` });

  if (!validation.ok) {
    progress.emit({ phase: 'Acceptance', state: 'failed', verified: true, detail: `Acceptance: static validation failed (${validation.errors[0].check}: ${validation.errors[0].message})` });
    return finish(operationNames.map(operation => operationRecord(operation, [{ name: 'static-validation', outcome: 'failed', elapsed_ms: 0, detail: `${validation.errors[0].check}: ${validation.errors[0].message}` }])));
  }

  if (manifest.sdk?.language !== 'node') {
    const reason = `acceptance execution harness supports node composition roots in this version; got ${manifest.sdk?.language || 'unknown'}`;
    progress.emit({ phase: 'Acceptance', state: 'advanced', verified: true, detail: `Acceptance: ${reason}` });
    return finish(operationNames.map(operation => operationRecord(operation, [{ name: 'harness', outcome: 'pending', elapsed_ms: 0, detail: reason }])));
  }

  // The harness boots the generated TypeScript composition root directly.
  // Node 23.6+ enables type stripping by default; maintained Node 22 releases
  // expose the same implementation behind the documented experimental flag.
  const [nodeMajor, nodeMinor] = process.versions.node.split('.').map(Number);
  const nodeArgs = nodeMajor > 23 || (nodeMajor === 23 && nodeMinor >= 6)
    ? []
    : nodeMajor === 22 && nodeMinor >= 6
      ? ['--experimental-strip-types']
      : null;
  if (!nodeArgs) {
    const reason = `acceptance harness needs Node 22.6+ to boot the generated TypeScript composition root; this runner is on ${process.versions.node}`;
    progress.emit({ phase: 'Acceptance', state: 'advanced', verified: true, detail: `Acceptance: ${reason}` });
    return finish(operationNames.map(operation => operationRecord(operation, [{ name: 'harness', outcome: 'pending', elapsed_ms: 0, detail: reason }])));
  }

  const keys = generateGatewayKeys();
  const installation = makeInstallation({ operations: operationNames, bindingDigest: installationBindingDigest(manifest, sha256Hex) });
  const trust = makeTrust(keys);
  const signer = new DevSigner(keys, installation);

  const { workspace, workRoot } = copyWorkspace(root);
  try {
    installNodeRuntime(workRoot, options);
    writeHarness(workRoot, manifest, { port: options.port || 0, hostname: 'acceptance.auteric.test', installation, trust, principals: PRINCIPALS });
    const server = await startServer(workRoot, { progress, reportPath, nodeArgs });
    try {
      const base = `http://127.0.0.1:${server.port}`;
      const call = (operation, params) => {
        const record = registry?.operations?.[operation];
        if (!record) throw Error(`no locked registry record for ${operation}`);
        return callOperation(base, signer, record, params);
      };
      const bound = new Set(operationNames);
      const operations = [];
      for (const operation of operationNames) {
        const record = registry?.operations?.[operation];
        if (!record) {
          operations.push(operationRecord(operation, [{ name: 'registry', outcome: 'pending', elapsed_ms: 0, detail: 'no locked registry record for this operation' }]));
          continue;
        }
        const plan = buildScenarioPlan(operation, record, { vectorsDir, boundOperations: bound });
        const executors = makeExecutors({ call, records: registry.operations, operation });
        const outcomes = [];
        for (const scenario of plan) {
          const started = Date.now();
          if (scenario.kind === 'pending') {
            outcomes.push({ name: scenario.name, outcome: 'pending', elapsed_ms: 0, detail: scenario.reason });
            continue;
          }
          const executor = executors[scenario.kind];
          if (!executor) {
            outcomes.push({ name: scenario.name, outcome: 'pending', elapsed_ms: 0, detail: `no executor for scenario kind ${scenario.kind}` });
            continue;
          }
          try {
            const detail = await executor(scenario);
            outcomes.push({ name: scenario.name, outcome: 'passed', elapsed_ms: Date.now() - started, detail });
          } catch (error) {
            const detail = error instanceof ScenarioFailure ? error.message : `runner error: ${error.message}`;
            outcomes.push({ name: scenario.name, outcome: 'failed', elapsed_ms: Date.now() - started, detail });
          }
        }
        const recordOut = operationRecord(operation, outcomes);
        operations.push(recordOut);
        const passed = outcomes.filter(outcome => outcome.outcome === 'passed').length;
        progress.emit({
          phase: 'Acceptance', operation, verified: true,
          state: recordOut.verdict === 'verified' ? 'complete' : recordOut.verdict === 'failed' ? 'failed' : 'advanced',
          detail: `Acceptance: ${operation} ${recordOut.verdict} (${passed}/${outcomes.length} scenarios over HTTP)${recordOut.reason ? ` — ${recordOut.reason}` : ''}`,
        });
      }
      const result = finish(operations);
      progress.emit({
        phase: 'Acceptance', state: result.summary.failed ? 'failed' : 'complete', verified: true,
        detail: `Acceptance: ${result.summary.verified}/${result.summary.total} operations verified; report at ${REPORT_PATH}`,
        evidence_ref: REPORT_PATH,
      });
      return result;
    } finally {
      await server.stop();
    }
  } finally {
    // Cleanup removes only the harness-created workspace, never the source.
    if (!options.keepWorkspace) rmSync(workspace, { recursive: true, force: true });
  }
}
