import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, realpathSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apiUrl, inspect, localStoreUrl, localTestDomain, prepareDiscovery, resolveProjectLayout, run } from '../src/cli.js';

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

test('one browser approval prepares a pending store and signals dashboard readiness', async () => {
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
    await run(['connect', '--localhost', '--no-browser'], root);
    assert.deepEqual(JSON.parse(readFileSync(join(root, '.well-known/ucp'), 'utf8')), document);
    assert.equal(JSON.parse(readFileSync(join(root, '.auteric/config.json'), 'utf8')).store_id, storeId);
    assert.equal(calls.find(call => call.path.endsWith('/cli/complete')).body.store_id, storeId);
    assert.equal(calls.find(call => call.path.endsWith('/cli/complete')).body.request_id, 'test-request-id');
  } finally {
    globalThis.fetch = previousFetch;
  }
});
