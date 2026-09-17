import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apiUrl, inspect, localStoreUrl, localTestDomain, prepareDiscovery, run } from '../src/cli.js';

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
  const root = mkdtempSync(join(tmpdir(), 'auteric-local-store-'));
  const domain = localTestDomain(root);
  assert.match(domain, /^local-[a-f0-9]{12}\.auteric\.test$/);
  assert.equal(localTestDomain(root), domain);
  await run(['connect', '--localhost', '--dry-run', '--store-url', 'http://127.0.0.1:5500'], root);
});

test('inspect custom static project and prepare only service-signed UCP', () => {
  const root = mkdtempSync(join(tmpdir(), 'auteric-cli-'));
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

test('discovery does not follow project symlinks', () => {
  const root = mkdtempSync(join(tmpdir(), 'auteric-symlink-'));
  const elsewhere = mkdtempSync(join(tmpdir(), 'auteric-elsewhere-'));
  symlinkSync(elsewhere, join(root, '.well-known'));
  assert.throws(() => prepareDiscovery(root, 'static', {
    ucp: { version: '2026-08-25' }, auteric_attestation: { signature: 'server-supplied' },
  }), /symlink/);
});

test('one browser approval prepares a pending store and signals dashboard readiness', async () => {
  const root = mkdtempSync(join(tmpdir(), 'auteric-guided-connect-'));
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
