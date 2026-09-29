import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { prepareServiceFirstBundle } from '../src/sidecar/bundle.js';

test('service-first bundle is deployable, secret-isolated and excludes payment', () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'auteric-sidecar-bundle-')));
  const runtimeConfig = {
    config_version: 'auteric-native-runtime/v1', release_id: 'release-test',
    installation: {
      installationId: 'installation_test', storeId: 'store_test', environment: 'sandbox',
      enabled: true, bindingDigest: 'sha256:' + 'a'.repeat(64), trustedProxyPrefix: null,
    },
    trust: { issuers: ['https://control.auteric.com'], keys: { test: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' } },
  };
  const verification = ['search_products', 'create_cart', 'create_checkout'].map(operation => ({
    operation, evidence_id: `evidence_${operation}`, passed_at: 1, expires_at: 4_102_444_800,
    test_suites: ['contract', 'merchant_e2e'],
  }));
  const result = prepareServiceFirstBundle(root, {
    runtimeConfig, merchantBaseUrl: 'http://127.0.0.1:9020', verification,
  });
  const sidecar = JSON.parse(readFileSync(join(result.directory, 'sidecar.json'), 'utf8'));
  const bridge = JSON.parse(readFileSync(join(result.directory, 'bridge.json'), 'utf8'));
  const compose = readFileSync(join(result.directory, 'compose.yaml'), 'utf8');
  assert.deepEqual(result.enabled_operations, ['search_products', 'create_cart', 'create_checkout']);
  assert.equal(sidecar.integration.profiles.create_cart.enabled, true);
  assert.equal(sidecar.integration.profiles.get_cart.enabled, false);
  assert.equal(sidecar.integration.profiles.complete_checkout, undefined);
  assert.equal(sidecar.installation.manifest.complete_checkout, undefined);
  assert.equal(sidecar.integration.profiles.create_cart.target.credential_ref, 'env:AUTERIC_BRIDGE_TOKEN');
  assert.equal(bridge.merchant_base_url, 'http://host.docker.internal:9020');
  assert.equal(bridge.allow_insecure_docker_host, true);
  assert.match(compose, /networks: \[auteric_private, merchant_backend\]/);
  assert.match(compose, /host\.docker\.internal:host-gateway/);
  assert.match(compose, /auteric_private: \{internal: true\}/);
  assert.match(compose, /merchant_backend: \{\}/);
  assert.equal(statSync(join(result.directory, '.env')).mode & 0o777, 0o600);
  assert.match(readFileSync(join(result.directory, 'README.md'), 'utf8'), /Never expose the bridge network/);
});
