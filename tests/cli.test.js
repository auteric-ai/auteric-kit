import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, realpathSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apiUrl, inspect, localStoreUrl, localTestDomain, prepareDiscovery, resolveProjectLayout, run, verifyLocalWithRetry } from '../src/cli.js';

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
    const result = await run(['connect', '--localhost', '--no-browser'], root);
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

test('local verification diagnoses wrong SPA fallback before calling control plane', async () => {
  const prior = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async target => { paths.push(new URL(target).pathname); return new Response('<html>SPA</html>', { status: 200 }); };
  try {
    const document = { ucp: { version: '2026-08-25' } };
    const result = await verifyLocalWithRetry('http://127.0.0.1:8100', 'store', 'test-token', 'http://127.0.0.1:5173', document, 1, async () => {});
    assert.equal(result.local_verified, false);
    assert.match(result.reason, /did not return JSON/);
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
