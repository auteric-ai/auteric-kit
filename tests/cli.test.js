import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, realpathSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apiUrl, detectLocalStoreUrl, existingDiscoveryDigest, inspect, localStoreUrl, localTestDomain, nativeBindingDigest, nativeReference, NATIVE_PHASE1_OPERATIONS, prepareBuiltDiscovery, prepareDiscovery, resolveProjectLayout, run, uncoveredOperations, verifyLocalWithRetry } from '../src/cli.js';
import { renderTerminalProgressLine, terminalColor } from '../src/progress.js';

test('terminal status colours are applied only when a terminal supports them', () => {
  assert.equal(terminalColor('success', 'green', { enabled: false }), 'success');
  assert.equal(terminalColor('success', 'green', { enabled: true }), '\x1b[32msuccess\x1b[0m');
  assert.equal(renderTerminalProgressLine({ elapsed_ms: 0, phase: 'Validation', state: 'complete' }, { color: true }), '\x1b[32m[00:00] Validation: complete\x1b[0m');
  assert.equal(renderTerminalProgressLine({ elapsed_ms: 0, phase: 'Validation', state: 'failed' }, { color: true }), '\x1b[31m[00:00] Validation: failed\x1b[0m');
});

test('localhost mode permits loopback only and cloud requires HTTPS', () => {
  assert.equal(apiUrl({ localhost: true }), 'http://127.0.0.1:8100');
  assert.throws(() => apiUrl({ localhost: true, 'api-url': 'http://192.168.1.2:8100' }), /loopback/);
  assert.throws(() => apiUrl({ localhost: true, 'api-url': 'https://control.auteric.com' }), /loopback/);
  assert.throws(() => apiUrl({ 'api-url': 'http://example.com' }), /HTTPS/);
  assert.throws(() => apiUrl({ localhost: true, 'api-url': 'http://localhost:8100/path' }), /origin/);
  assert.equal(localStoreUrl('http://127.0.0.1:5500'), 'http://127.0.0.1:5500');
  assert.throws(() => localStoreUrl('http://example.com:5500'), /loopback/);
});

test('local store has a stable non-public test identifier without a domain', async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-local-store-'));
  const domain = localTestDomain(root);
  assert.match(domain, /^local-[a-f0-9]{12}\.auteric\.test$/);
  assert.equal(localTestDomain(root), domain);
  await run(['connect', '--localhost', '--dry-run', '--store-url', 'http://127.0.0.1:5500'], root);
});

test('flags without a subcommand use the Connect workflow for GitHub npx', async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-github-shortcut-'));
  await run(['--localhost', '--dry-run', '--store-url', 'http://127.0.0.1:5500'], root);
});

test('cloud dry run accepts a merchant domain without a local API origin', async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-cloud-store-'));
  await run(['connect', '--domain', 'shop.example', '--dry-run'], root);
});

test('local-storefront finds the merchant service while preserving the cloud control-plane', async () => {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async target => {
    calls.push(String(target));
    return new Response('', { status: String(target).endsWith(':9020/api/health') ? 200 : 404 });
  };
  try {
    assert.equal(await detectLocalStoreUrl(), 'http://127.0.0.1:9020');
    const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-local-cloud-'));
    await run(['connect', '--domain', 'shop.example', '--local-storefront', '--dry-run', '--no-agent'], root);
    assert.equal(calls[0], 'http://127.0.0.1:9020/api/health');
  } finally { globalThis.fetch = previous; }
});

test('candidate operations outside an existing connector remain completion work', () => {
  const inventory = { capability_coverage: [
    { operation: 'search_products', status: 'candidate' },
    { operation: 'get_product', status: 'candidate' },
    { operation: 'create_cart', status: 'candidate' },
    { operation: 'payment_capture', status: 'unsupported' },
  ] };
  assert.deepEqual(uncoveredOperations(inventory, ['search_products', 'get_product']), ['create_cart']);
});

test('project root selects one backend and frontend automatically', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-layout-'));
  const api = join(root, 'services', 'api');
  const web = join(root, 'apps', 'web');
  mkdirSync(api, { recursive: true });
  mkdirSync(web, { recursive: true });
  writeFileSync(join(api, 'package.json'), JSON.stringify({ dependencies: { express: '5.0.0' } }));
  writeFileSync(join(web, 'package.json'), JSON.stringify({ dependencies: { next: '15.0.0' } }));
  const layout = resolveProjectLayout(root);
  assert.equal(layout.backend, api);
  assert.equal(layout.frontend, web);
  assert.equal(layout.backendRelative, 'services/api');
  assert.equal(layout.frontendRelative, 'apps/web');
});

test('project root requires an explicit backend selection when there are multiple APIs', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-layout-many-'));
  for (const name of ['catalog', 'checkout']) {
    const api = join(root, 'services', name);
    mkdirSync(api, { recursive: true });
    writeFileSync(join(api, 'package.json'), JSON.stringify({ dependencies: { express: '5.0.0' } }));
  }
  assert.throws(() => resolveProjectLayout(root), /multiple backend candidates/);
  assert.equal(resolveProjectLayout(root, { backend: 'services/catalog' }).backend, join(root, 'services/catalog'));
});

test('inspect custom static project and prepare only service-signed UCP', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-cli-'));
  writeFileSync(join(root, 'index.html'), '<h1>Shop</h1>');
  assert.equal(inspect(root).framework, 'static');
  const document = { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'server-provided-signature' } };
  const result = prepareDiscovery(root, 'static', document);
  assert.match(result.path, /\.well-known\/ucp$/);
  assert.doesNotMatch(result.path, /public\/\.well-known\/ucp$/);
  assert.deepEqual(JSON.parse(readFileSync(result.path, 'utf8')), document);
  assert.equal(prepareDiscovery(root, 'static', document).changed, false);
  assert.throws(() => prepareDiscovery(root, 'static', { ...document, ucp: { version: 'different' } }), /no file was overwritten/);
  assert.throws(() => prepareDiscovery(root, 'static', { ucp: { version: '2026-08-25' } }), /signed/);
});

test('Vite publishes discovery from the untransformed public directory', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-vite-'));
  writeFileSync(join(root, 'package.json'), JSON.stringify({ devDependencies: { vite: '6.0.0' } }));
  const document = { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'server-provided-signature' } };
  assert.equal(inspect(root).framework, 'vite');
  const result = prepareDiscovery(root, 'vite', document);
  assert.match(result.path, /public\/\.well-known\/ucp$/);
  assert.deepEqual(JSON.parse(readFileSync(result.path, 'utf8')), document);
});

test('existing production build receives the same signed profile without replacing unrelated data', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-built-'));
  const built = join(root, 'dist');
  mkdirSync(built);
  writeFileSync(join(built, 'index.html'), '<h1>Shop</h1>');
  const document = { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'signed' } };
  const first = prepareBuiltDiscovery(root, document);
  assert.equal(first.path, join(built, '.well-known', 'ucp'));
  assert.deepEqual(JSON.parse(readFileSync(first.path, 'utf8')), document);
  assert.equal(prepareBuiltDiscovery(root, document).changed, false);
  assert.throws(() => prepareBuiltDiscovery(root, { ...document, ucp: { version: 'new' } }), /no file was overwritten/);
});

test('recoverable UCP profile belongs to the exact Store only', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-recovery-'));
  const document = { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'signed', payload: { store_id: 'store-a' } } };
  prepareDiscovery(root, 'vite', document);
  assert.match(existingDiscoveryDigest(root, 'vite', 'store-a'), /^[a-f0-9]{64}$/);
  assert.equal(existingDiscoveryDigest(root, 'vite', 'store-b'), null);
});

test('discovery does not follow project symlinks', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-symlink-'));
  const elsewhere = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-elsewhere-'));
  symlinkSync(elsewhere, join(root, '.well-known'));
  assert.throws(() => prepareDiscovery(root, 'static', {
    ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'server-supplied' },
  }), /symlink/);
});

test('missing implementation stops before authorization, Store creation and UCP generation', async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-guided-connect-'));
  const previousFetch = globalThis.fetch;
  const storeId = 'a'.repeat(32);
  const calls = [];
  const document = { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'service-signature' } };
  globalThis.fetch = async (target, options = {}) => {
    const path = new URL(target).pathname;
    calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
    const data = path.endsWith('/cli/start')
      ? { authorization_url: 'http://127.0.0.1:8100/cli/authorize?request=test',
          request_id: 'test-request-id', user_code: '1234-5678', expires_at: Date.now() / 1000 + 60, interval: 0 }
      : path.endsWith('/cli/poll')
        ? { status: 'authorized', access_token: 'test-token', user: { email: 'owner@example.com', organization: 'Owner' } }
        : path.endsWith('/cli/complete')
          ? { completed: true, store_id: storeId }
          : path.endsWith('/discovery')
            ? { document }
            : path.endsWith('/stores') && options.method === 'POST'
              ? { id: storeId }
              : path.endsWith('/stores') ? [] : {};
    return new Response(JSON.stringify(data), { status: 200, headers: { 'content-type': 'application/json' } });
  };
  try {
    const result = await run(['connect', '--localhost', '--no-browser', '--no-agent'], root);
    assert.equal(result.integration, 'implementation_required');
    assert.equal(existsSync(join(root, '.well-known/ucp')), false);
    assert.equal(existsSync(join(root, '.auteric/config.json')), false);
    assert.deepEqual(calls, []);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('local verification retries a temporarily unavailable route and preserves exact UCP', async () => {
  const prior = globalThis.fetch;
  const document = { ucp: { version: '2026-08-25', capabilities: { catalog: true } }, auteric_attestation: { signature: 'signed' } };
  let verificationCalls = 0;
  globalThis.fetch = async target => {
    const path = new URL(target).pathname;
    if (path === '/.well-known/ucp') return Response.json(document);
    verificationCalls++;
    return verificationCalls < 3
      ? Response.json({ detail: 'Local UCP route is unavailable or invalid' }, { status: 422 })
      : Response.json({ local_verified: true, public_domain_verified: false, url: 'http://127.0.0.1:5173/.well-known/ucp' });
  };
  try {
    const result = await verifyLocalWithRetry('http://127.0.0.1:8100', 'store', 'test-token', 'http://127.0.0.1:5173', document, 4, async () => {});
    assert.equal(result.local_verified, true);
    assert.equal(verificationCalls, 3);
  } finally { globalThis.fetch = prior; }
});

test('local discovery rejects a JSON body served with the wrong MIME type', async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ ucp: { version: '2026-08-25' } }),
    { headers: { 'content-type': 'application/octet-stream' } });
  try {
    const result = await verifyLocalWithRetry('http://127.0.0.1:8100', 'store', 'token',
      'http://127.0.0.1:9020', { ucp: { version: '2026-08-25' } }, 1);
    assert.equal(result.local_verified, false);
    assert.match(result.reason, /Content-Type: application\/json/);
  } finally { globalThis.fetch = previous; }
});

test('local verification diagnoses wrong SPA fallback before calling control plane', async () => {
  const prior = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async target => { paths.push(new URL(target).pathname); return new Response('<html>SPA</html>', { status: 200 }); };
  try {
    const document = { ucp: { version: '2026-08-25' } };
    const result = await verifyLocalWithRetry('http://127.0.0.1:8100', 'store', 'test-token', 'http://127.0.0.1:5173', document, 1, async () => {});
    assert.equal(result.local_verified, false);
    assert.match(result.reason, /Content-Type: application\/json/);
    assert.deepEqual(paths, ['/.well-known/ucp']);
  } finally { globalThis.fetch = prior; }
});

test('Vite, Next, and static deployments use their public discovery directory', () => {
  for (const framework of ['vite', 'next', 'static']) {
    const root = mkdtempSync(join(realpathSync(tmpdir()), `auteric-${framework}-`));
    const result = prepareDiscovery(root, framework, { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'signed' } });
    assert.equal(result.path.includes('/public/'), framework !== 'static');
  }
});

test('native reference Connect registers and verifies Native HTTP without invoking connector flow', async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-native-reference-'));
  mkdirSync(join(root, 'server', 'auteric'), { recursive: true });
  writeFileSync(join(root, 'package.json'), JSON.stringify({
    type: 'module', dependencies: { express: '4.21.2', '@auteric/merchant-node': '0.1.0' }, scripts: {},
  }));
  writeFileSync(join(root, 'server', 'auteric', 'runtime.js'), 'export const nativeRuntime = true;\n');
  writeFileSync(join(root, 'server', 'app.js'), "app.use('/api/auteric/v1', nativeRouter);\n");
  assert.equal(nativeReference(root).detected, true);
  const digest = nativeBindingDigest(root);
  const previousFetch = globalThis.fetch;
  const calls = [];
  let runtimeReachable = true;
  let storeExists = false;
  const storeId = 'b'.repeat(32);
  const installationId = 'install_reference';
  const document = { ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'service-signature' } };
  globalThis.fetch = async (target, init = {}) => {
    const path = new URL(target).pathname;
    calls.push(path);
    let body = {};
    if (init.body) body = JSON.parse(init.body);
    if (path.endsWith('/cli/start')) return Response.json({ authorization_url: 'https://control.auteric.com/cli/authorize?request=test', request_id: 'request', expires_at: Date.now() / 1000 + 30, interval: 0 });
    if (path.endsWith('/cli/poll')) return Response.json({ status: 'authorized', access_token: 'token', user: { email: 'owner@example.com', organization: 'Owner' } });
    if (path.endsWith('/stores') && (!init.method || init.method === 'GET')) {
      return Response.json(storeExists ? [{ id: storeId, domain: 'native.example', environment: 'production' }] : []);
    }
    if (path.endsWith('/stores') && init.method === 'POST') {
      storeExists = true;
      return Response.json({ id: storeId, domain: 'native.example', environment: 'production' });
    }
    if (path.endsWith('/installations') && init.method === 'POST') {
      assert.equal(body.transport, 'native_http');
      assert.deepEqual(body.operations.map(item => item.operation), NATIVE_PHASE1_OPERATIONS);
      assert.equal(body.native_runtime.binding_digest, digest);
      return Response.json({ id: installationId, transport: 'native_http', endpoint: 'https://native.example', environment: 'production' });
    }
    if (path.endsWith('/native-runtime-config')) return Response.json({
      config_version: 'auteric-native-runtime/v1', endpoint: 'https://native.example', release_id: 'release',
      installation: { installationId, storeId, environment: 'production', enabled: true, bindingDigest: digest, operations: NATIVE_PHASE1_OPERATIONS },
      trust: { issuers: ['https://control.auteric.com'], keys: { test: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' } },
    });
    if (path.endsWith(`/installations/${installationId}/verify`)) return Response.json({ reachable: runtimeReachable });
    if (/\/capabilities\/[^/]+\/enable$/.test(path)) return Response.json({ enabled: true });
    if (path.endsWith('/connection-health')) return Response.json({ checks: { policies: { state: 'active' } } });
    if (path.endsWith('/discovery')) return Response.json({ document });
    if (path.endsWith(`/stores/${storeId}/verify`)) return Response.json({ verified: true });
    if (path.endsWith('/agent-access') && init.method === 'PUT') return Response.json({ enabled: true });
    throw Error(`unexpected fetch ${path}`);
  };
  try {
    const result = await run(['connect', '--domain', 'native.example', '--no-browser', '--no-agent'], root);
    assert.equal(result.integration, 'native_http_verified');
    assert.equal(result.public_discovery_verified, true);
    assert.equal(result.status, 'connection_test_required');
    assert.ok(existsSync(join(root, '.auteric', 'native-runtime.json')));
    assert.ok(existsSync(join(root, '.auteric', 'native-installation.json')));
    assert.ok(existsSync(join(root, '.well-known', 'ucp')));
    assert.equal(calls.some(path => /\/mappings|\/mcp-credentials|\/connector/.test(path)), false);
    assert.equal(calls.some(path => path.endsWith('/agent-access')), false);

    // A newly connected clean merchant cannot be live until it deploys the
    // generated runtime config and UCP. That expected state must still leave
    // both artifacts ready for one deployment; it must not require Connect to
    // be run a second time merely to obtain the signed profile.
    runtimeReachable = false;
    calls.length = 0;
    const pending = await run(['connect', '--domain', 'native.example', '--no-browser', '--no-agent'], root);
    assert.equal(pending.status, 'deployment_pending');
    assert.equal(pending.public_discovery_verified, false);
    assert.ok(existsSync(join(root, '.auteric', 'native-runtime.json')));
    assert.ok(existsSync(join(root, '.well-known', 'ucp')));
    assert.ok(calls.indexOf(`/api/commerce/stores/${storeId}/discovery`) < calls.indexOf(`/api/commerce/stores/${storeId}/installations/${installationId}/verify`));
    assert.equal(calls.some(path => path.endsWith(`/stores/${storeId}/verify`)), false);
  } finally { globalThis.fetch = previousFetch; }
});
