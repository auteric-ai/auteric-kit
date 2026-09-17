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
