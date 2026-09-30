import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { prepareEcsMvpArtifacts, sidecarBundleDigest } from '../src/deployment.js';

test('durable artifacts declare shared storage and reject an ephemeral ledger', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-durable-'));
  const config = { schema: 'auteric-sidecar/v1', operational: {
    storage_mode: 'durable', execution_store: 'postgres', audit_store: 'postgres',
    database_secret_ref: 'env:AUTERIC_SIDECAR_DATABASE_URL',
  } };
  const options = { installationId: 'install_durable', domain: 'shop.example', operations: ['create_cart'] };
  const result = prepareEcsMvpArtifacts(root, config, options);
  const manifest = JSON.parse(readFileSync(result.manifestPath));
  assert.equal(manifest.storage_mode, 'durable');
  assert.ok(manifest.deployment_constraints.includes('shared_postgres_required'));
  assert.ok(!manifest.deployment_constraints.includes('desired_count=1'));
  assert.throws(() => prepareEcsMvpArtifacts(root, {
    ...config, operational: { ...config.operational, execution_store: 'sqlite:/tmp/ledger' },
  }, options), /shared PostgreSQL/);
});

test('ECS MVP artifacts are tracked public configuration, never session data', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-deployment-'));
  const config = { schema: 'auteric-sidecar/v1', operational: { storage_mode: 'ephemeral' }, installation: {} };
  const result = prepareEcsMvpArtifacts(root, config, { installationId: 'install_1', domain: 'shop.example', operations: ['search_products'] });
  assert.deepEqual(result.artifacts, ['auteric/sidecar.json', 'auteric/deployment.json']);
  const manifest = JSON.parse(readFileSync(result.manifestPath));
  assert.equal(manifest.storage_mode, 'ephemeral');
  assert.equal(manifest.sidecar_bundle_digest, sidecarBundleDigest(config));
});

test('an unchanged generated bundle can be reconciled with refreshed public policy', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-deployment-rerun-'));
  const initial = { schema: 'auteric-sidecar/v1', operational: { storage_mode: 'ephemeral' }, installation: {} };
  prepareEcsMvpArtifacts(root, initial, { installationId: 'install_1', domain: 'shop.example', operations: ['search_products'] });
  const refreshed = { ...initial, trust: { keys: { current: 'public-key' } } };
  const result = prepareEcsMvpArtifacts(root, refreshed, { installationId: 'install_1', domain: 'shop.example', operations: ['search_products'] });
  assert.deepEqual(JSON.parse(readFileSync(result.configPath, 'utf8')), refreshed);
  assert.equal(JSON.parse(readFileSync(result.manifestPath, 'utf8')).sidecar_bundle_digest, sidecarBundleDigest(refreshed));
});

test('Connect refuses to overwrite a hand-edited public sidecar config', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-deployment-conflict-'));
  const config = { schema: 'auteric-sidecar/v1', operational: { storage_mode: 'ephemeral' }, installation: {} };
  const result = prepareEcsMvpArtifacts(root, config, { installationId: 'install_1', domain: 'shop.example', operations: ['search_products'] });
  writeFileSync(result.configPath, JSON.stringify({ ...config, edited: true }));
  assert.throws(() => prepareEcsMvpArtifacts(root, config, {
    installationId: 'install_1', domain: 'shop.example', operations: ['search_products'],
  }), /changed outside Connect/);
});

test('artifacts cannot be reused across stores or installations', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'auteric-deployment-owner-'));
  const config = { schema: 'auteric-sidecar/v1', operational: { storage_mode: 'ephemeral' }, installation: {} };
  prepareEcsMvpArtifacts(root, config, { installationId: 'install_1', domain: 'shop.example', operations: ['search_products'] });
  assert.throws(() => prepareEcsMvpArtifacts(root, config, {
    installationId: 'install_2', domain: 'other.example', operations: ['search_products'],
  }), /another installation or domain/);
});
