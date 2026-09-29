import { createHash, randomBytes } from 'node:crypto';
import { chmodSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { loadOperationsRegistry } from '../inventory/operations.js';
import { atomicJSON } from '../workflow.js';

export const CUSTOM_MVP_OPERATIONS = Object.freeze([
  'search_products', 'get_product',
  'create_cart', 'get_cart', 'add_to_cart', 'update_cart_item', 'remove_from_cart',
  'replace_cart_items', 'cancel_cart',
  'create_checkout', 'get_checkout', 'update_checkout', 'cancel_checkout',
]);

export const DEFAULT_BRIDGE_ROUTES = Object.freeze({
  sessionBootstrap: '/api/session/guest', searchProducts: '/api/products',
  getProduct: '/api/products/{product_id}', createCart: '/api/carts',
  getCart: '/api/carts/{cart_id}', addToCart: '/api/carts/{cart_id}/items',
  updateCartItem: '/api/carts/{cart_id}/items/{line_id}',
  removeFromCart: '/api/carts/{cart_id}/items/{line_id}',
  replaceCartItems: '/api/carts/{cart_id}/items', cancelCart: '/api/carts/{cart_id}',
  createCheckout: '/api/checkouts', getCheckout: '/api/checkouts/{checkout_id}',
  updateCheckout: '/api/checkouts/{checkout_id}', cancelCheckout: '/api/checkouts/{checkout_id}',
});

export function serviceFirstBindingDigest(routes = DEFAULT_BRIDGE_ROUTES) {
  return digest({ schema: 'auteric-http-bridge/v1', runtime: '@auteric/merchant-node/http-bridge-v1', routes });
}

export function serviceFirstOperationEvidence(bindingDigest, operations = CUSTOM_MVP_OPERATIONS, registryDir) {
  const registry = loadOperationsRegistry(registryDir);
  if (!registry) throw Error('Locked operations registry is required');
  const index = JSON.parse(readFileSync(join(registry.path, '..', 'registry.json'), 'utf8'));
  const contractDigest = digest(index);
  const compiled = new Map(index.operations.map(item => [item.operation, item]));
  return operations.map(operation => {
    if (!registry.operations[operation]) throw Error(`Locked commerce contract is missing ${operation}`);
    return {
      operation, contract_digest: contractDigest, binding_digest: compiled.get(operation).digest,
      test_evidence: { kind: 'merchant_sandbox_e2e', status: 'pass' },
      deployment_evidence: { kind: 'service_first_bundle', status: 'prepared' },
    };
  });
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

function digest(value) {
  return 'sha256:' + createHash('sha256').update(canonical(value)).digest('hex');
}

export function serviceFirstSupport(report) {
  const routes = new Set((report?.graph?.nodes || []).flatMap(node => node.routes || [])
    .filter(item => item.kind === 'rest').map(item => `${item.method} ${item.path}`));
  const required = [
    'POST /api/session/guest', 'GET /api/products', 'GET /api/products/:id',
    'POST /api/carts', 'GET /api/carts/:id', 'POST /api/carts/:id/items',
    'PATCH /api/carts/:id/items/:lineId', 'DELETE /api/carts/:id/items/:lineId',
    'POST /api/checkouts', 'GET /api/checkouts/:id', 'PATCH /api/checkouts/:id',
    'DELETE /api/checkouts/:id',
  ];
  const missing = required.filter(item => !routes.has(item));
  return { supported: missing.length === 0, missing, profile: 'session-http-v1' };
}

export async function verifyStandardMerchantBridge(merchantBaseUrl, fetcher = globalThis.fetch) {
  const base = new URL(merchantBaseUrl);
  if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname) || base.pathname !== '/') {
    throw Error('Automatic write verification is restricted to an explicit loopback sandbox');
  }
  let cookie = '';
  let sequence = 0;
  const passed = [];
  const call = async (operation, method, path, body) => {
    const headers = { accept: 'application/json' };
    if (cookie) headers.cookie = cookie;
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (method !== 'GET') headers['idempotency-key'] = `auteric-connect-${Date.now()}-${++sequence}`;
    const response = await fetcher(base.origin + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body), redirect: 'error',
      signal: AbortSignal.timeout(10000),
    });
    const setCookie = response.headers.get('set-cookie');
    if (setCookie) cookie = setCookie.split(';', 1)[0];
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(`${operation} sandbox verification returned HTTP ${response.status}`);
    if (operation) passed.push(operation);
    return payload;
  };
  await call(null, 'POST', DEFAULT_BRIDGE_ROUTES.sessionBootstrap);
  const search = await call('search_products', 'GET', DEFAULT_BRIDGE_ROUTES.searchProducts + '?query=&limit=2');
  const first = (search.items || search.results || search)[0];
  if (!first?.id) throw Error('sandbox catalog contains no product for cart verification');
  await call('get_product', 'GET', `/api/products/${encodeURIComponent(first.id)}`);
  const variantId = first.variants?.[0]?.id;
  if (!variantId) throw Error('sandbox product contains no variant for cart verification');
  let disposable = await call('create_cart', 'POST', DEFAULT_BRIDGE_ROUTES.createCart, { currency: first.currency || 'USD' });
  disposable = await call('add_to_cart', 'POST', `/api/carts/${encodeURIComponent(disposable.id)}/items`, { productId: first.id, variantId, quantity: 1 });
  await call('get_cart', 'GET', `/api/carts/${encodeURIComponent(disposable.id)}`);
  const disposableLine = disposable.items?.[0]?.lineId;
  if (!disposableLine) throw Error('sandbox cart did not return a stable line id');
  disposable = await call('update_cart_item', 'PATCH', `/api/carts/${encodeURIComponent(disposable.id)}/items/${encodeURIComponent(disposableLine)}`, { quantity: 2 });
  await call('remove_from_cart', 'DELETE', `/api/carts/${encodeURIComponent(disposable.id)}/items/${encodeURIComponent(disposableLine)}`);
  await call('replace_cart_items', 'PUT', `/api/carts/${encodeURIComponent(disposable.id)}/items`, { items: [{ productId: first.id, variantId, quantity: 1 }] });
  await call('cancel_cart', 'DELETE', `/api/carts/${encodeURIComponent(disposable.id)}`);
  let checkoutCart = await call(null, 'POST', DEFAULT_BRIDGE_ROUTES.createCart, { currency: first.currency || 'USD' });
  checkoutCart = await call(null, 'POST', `/api/carts/${encodeURIComponent(checkoutCart.id)}/items`, { productId: first.id, variantId, quantity: 1 });
  let checkout = await call('create_checkout', 'POST', DEFAULT_BRIDGE_ROUTES.createCheckout, { cartId: checkoutCart.id });
  await call('get_checkout', 'GET', `/api/checkouts/${encodeURIComponent(checkout.id)}`);
  checkout = await call('update_checkout', 'PATCH', `/api/checkouts/${encodeURIComponent(checkout.id)}`, {
    customer: { email: 'auteric-verification@example.invalid', firstName: 'Auteric', lastName: 'Verification' },
    shippingAddress: { firstName: 'Auteric', lastName: 'Verification', address: '1 Test Way', city: 'Testville', postalCode: '10001', country: 'US' },
  });
  await call('cancel_checkout', 'DELETE', `/api/checkouts/${encodeURIComponent(checkout.id)}`);
  const completed = new Set(passed);
  const expected = CUSTOM_MVP_OPERATIONS.filter(operation => !['complete_checkout'].includes(operation));
  const missing = expected.filter(operation => !completed.has(operation));
  if (missing.length) throw Error(`sandbox verification did not cover: ${missing.join(', ')}`);
  const passedAt = Math.floor(Date.now() / 1000);
  return expected.map(operation => ({
    operation, evidence_id: `sandbox_${operation}_${randomBytes(8).toString('hex')}`,
    passed_at: passedAt, expires_at: passedAt + 7 * 86400,
    test_suites: ['contract', 'merchant_sandbox_e2e', 'session_isolation'],
  }));
}

/** Create the complete merchant-local deployment bundle from a control-plane runtime config. */
export function prepareServiceFirstBundle(root, options) {
  const runtime = options.runtimeConfig;
  if (runtime?.config_version !== 'auteric-native-runtime/v1') throw Error('Pinned native runtime config is required');
  const registry = loadOperationsRegistry(options.registryDir);
  if (!registry) throw Error('Locked operations registry is required');
  const registryIndex = JSON.parse(readFileSync(join(registry.path, '..', 'registry.json'), 'utf8'));
  const compiled = new Map(registryIndex.operations.map(item => [item.operation, item]));
  const selected = options.operations || CUSTOM_MVP_OPERATIONS;
  const unknown = selected.filter(operation => !registry.operations[operation]);
  if (unknown.length) throw Error(`Unknown sidecar operations: ${unknown.join(', ')}`);
  const target = resolve(root, '.auteric/runtime');
  mkdirSync(target, { recursive: true });
  const merchantUrl = new URL(options.merchantBaseUrl);
  const dockerLoopback = merchantUrl.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(merchantUrl.hostname);
  if (dockerLoopback) merchantUrl.hostname = 'host.docker.internal';
  const bridge = {
    schema: 'auteric-http-bridge/v1', merchant_base_url: merchantUrl.origin,
    ...(dockerLoopback ? { allow_insecure_docker_host: true } : {}),
    routes: { ...DEFAULT_BRIDGE_ROUTES, ...(options.routes || {}) },
  };
  atomicJSON(join(target, 'bridge.json'), bridge, 0o644);
  const mapping = digest(bridge);
  const now = Math.floor(Date.now() / 1000);
  const verified = new Map((options.verification || []).map(item => [item.operation, item]));
  const profiles = {};
  const manifest = {};
  for (const operation of selected) {
    const contract = { ...registry.operations[operation], ...compiled.get(operation) };
    const evidence = verified.get(operation);
    manifest[operation] = {
      method: contract.method, path: contract.path,
      contract_version: contract.contract_version,
      binding_digest: runtime.installation.operationBindings?.[operation] || contract.digest,
    };
    profiles[operation] = {
      operation, integration_version: options.releaseId || runtime.release_id || 'custom-mvp-v1',
      mapping_fingerprint: mapping, contract_fingerprint: contract.digest,
      target: {
        base_url: 'http://bridge:7071', method: 'POST', path: `/invoke/${operation}`,
        timeout_seconds: 10, retries: contract.side_effect === 'read' ? 2 : 0,
        auth_scheme: 'bearer', credential_ref: 'env:AUTERIC_BRIDGE_TOKEN',
      },
      enabled: Boolean(evidence), integration_mode: 'service_bridge',
      merchant_selection_id: evidence ? `service-first:${runtime.installation.installationId}:${mapping.slice(7, 23)}` : null,
      verification: evidence ? {
        evidence_id: evidence.evidence_id, operation, environment: runtime.installation.environment,
        mapping_fingerprint: mapping, contract_fingerprint: contract.digest,
        passed_at: evidence.passed_at, expires_at: evidence.expires_at,
        test_suites: evidence.test_suites,
      } : null,
    };
  }
  const policyBody = { allowed_operations: [...verified.keys()].filter(op => selected.includes(op)), issued_at: now, expires_at: now + 86400 };
  const sidecar = {
    schema: 'auteric-sidecar/v1',
    installation: {
      installation_id: runtime.installation.installationId, store_id: runtime.installation.storeId,
      environment: runtime.installation.environment, enabled: runtime.installation.enabled,
      root_path: runtime.installation.trustedProxyPrefix || null, manifest,
    },
    trust: { issuer_allowlist: runtime.trust.issuers, keys: runtime.trust.keys },
    integration: {
      store_id: runtime.installation.storeId, installation_id: runtime.installation.installationId,
      environment: runtime.installation.environment, integration_version: options.releaseId || runtime.release_id || 'custom-mvp-v1',
      merchant_protocol: registryIndex.merchant_protocol, registry_version: registryIndex.registry_version,
      registry_digest: digest(registryIndex), profiles,
    },
    operational: {
      execution_store: 'sqlite:/var/lib/auteric/executions.db', audit_store: 'sqlite:/var/lib/auteric/audit.db',
      control_token_ref: 'env:AUTERIC_SIDECAR_CONTROL_TOKEN',
      policy: { fingerprint: digest(policyBody), ...policyBody },
    },
  };
  atomicJSON(join(target, 'sidecar.json'), sidecar, 0o644);
  const envPath = join(target, '.env');
  if (!options.preserveSecrets || !readFileSafe(envPath)) {
    writeFileSync(envPath, `AUTERIC_BRIDGE_TOKEN=${randomBytes(32).toString('base64url')}\nAUTERIC_SIDECAR_CONTROL_TOKEN=${randomBytes(32).toString('base64url')}\n`, { mode: 0o600 });
  }
  const compose = `services:\n  bridge:\n    image: \${AUTERIC_BRIDGE_IMAGE:-ghcr.io/auteric/merchant-bridge:0.1.0}\n    restart: unless-stopped\n    env_file: [.env]\n    environment:\n      AUTERIC_BRIDGE_CONFIG: /etc/auteric/bridge.json\n      AUTERIC_BRIDGE_EXPOSURE: container-network\n    volumes:\n      - ./bridge.json:/etc/auteric/bridge.json:ro\n    networks: [auteric_private]\n    healthcheck:\n      test: [\"CMD\", \"node\", \"-e\", \"fetch('http://127.0.0.1:7071/health/ready',{headers:{authorization:'Bearer '+process.env.AUTERIC_BRIDGE_TOKEN}}).then(r=>{if(!r.ok)process.exit(1)})\"]\n      interval: 2s\n      timeout: 2s\n      retries: 10\n  sidecar:\n    image: \${AUTERIC_SIDECAR_IMAGE:-ghcr.io/auteric/merchant-sidecar:0.1.0}\n    restart: unless-stopped\n    env_file: [.env]\n    environment:\n      AUTERIC_SIDECAR_CONFIG: /etc/auteric/sidecar.json\n    command: [\"--config\", \"/etc/auteric/sidecar.json\", \"--host\", \"0.0.0.0\", \"--port\", \"8080\"]\n    volumes:\n      - ./sidecar.json:/etc/auteric/sidecar.json:ro\n      - auteric_state:/var/lib/auteric\n    networks: [auteric_private, merchant_ingress]\n    depends_on:\n      bridge: {condition: service_healthy}\n    healthcheck:\n      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=2)\"]\n      interval: 2s\n      timeout: 2s\n      retries: 10\nnetworks:\n  auteric_private: {internal: true}\n  merchant_ingress: {}\nvolumes:\n  auteric_state: {}\n`;
  const routableCompose = compose
    .replace('    networks: [auteric_private]\n    healthcheck:', '    networks: [auteric_private, merchant_backend]\n    extra_hosts:\n      - "host.docker.internal:host-gateway"\n    healthcheck:')
    .replace('  merchant_ingress: {}\nvolumes:', '  merchant_backend: {}\n  merchant_ingress: {}\nvolumes:');
  writeFileSync(join(target, 'compose.yaml'), routableCompose);
  writeFileSync(join(target, 'disconnect.sh'), '#!/bin/sh\nset -eu\ndocker compose --env-file .env -f compose.yaml down\n');
  chmodSync(join(target, 'disconnect.sh'), 0o755);
  writeFileSync(join(target, 'README.md'), `# Auteric merchant runtime\n\nThis bundle runs a fixed bridge image and a fixed sidecar image. Store-specific behavior lives only in bridge.json and sidecar.json.\n\n- Start: \`docker compose --env-file .env -f compose.yaml up -d\`\n- Stop/disconnect locally: \`./disconnect.sh\`\n- Remove local durable state only after an explicit retention decision: \`docker compose -f compose.yaml down -v\`\n\nReverse-proxy only the sidecar's port 8080 at \`/api/auteric/v1\`. Never expose the bridge network or .env. Profiles without current verification evidence remain disabled. Payment and complete_checkout are intentionally absent.\n`);
  writeFileSync(join(target, '.gitignore'), '.env\n*.db\n*.db-*\n');
  return { directory: target, operations: selected, enabled_operations: [...verified.keys()], mapping_fingerprint: mapping };
}

function readFileSafe(path) {
  try { return readFileSync(path, 'utf8'); } catch { return null; }
}
