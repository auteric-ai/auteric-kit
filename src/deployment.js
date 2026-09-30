// Git-tracked deployment contract for the first supported Custom Store ECS
// path. Authentication sessions and reports remain in .auteric; this module
// writes only public configuration returned by the control plane.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { atomicJSON } from './workflow.js';

// `credential_ref: env:AUTERIC_BRIDGE_TOKEN` is a public indirection that the
// task resolves at runtime; it is not a credential value.  Keep rejecting
// values that look like actual tokens/private material.
const FORBIDDEN = /(?:access[_-]?token\s*[:=]\s*["'][^"']+|private[_-]?key|secret\s*[:=]\s*["'][^"']+)/i;

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

export function sidecarBundleDigest(config) {
  return 'sha256:' + createHash('sha256').update(JSON.stringify(canonical(config))).digest('hex');
}

function readExistingJson(path, label) {
  try { return JSON.parse(readFileSync(path, 'utf8')); }
  catch { throw Error(`Existing ${label} is not valid JSON; reconcile it before running Connect again`); }
}

function assertReconciled(configPath, manifestPath, nextConfig, installationId, domain) {
  if (!existsSync(configPath) && !existsSync(manifestPath)) return;
  if (!existsSync(configPath) || !existsSync(manifestPath)) {
    throw Error('Existing Auteric deployment artifacts are incomplete; reconcile auteric/sidecar.json and auteric/deployment.json before running Connect again');
  }
  const existingConfig = readExistingJson(configPath, 'sidecar configuration');
  const existingManifest = readExistingJson(manifestPath, 'deployment manifest');
  if (existingManifest.schema !== 'auteric-ecs-mvp/v1' ||
      existingManifest.installation_id !== installationId || existingManifest.domain !== domain) {
    throw Error('Existing Auteric deployment artifacts belong to another installation or domain; reconcile them before running Connect again');
  }
  // The manifest records exactly what Connect wrote.  A mismatch means a
  // person edited the public config, so never silently replace that edit.
  if (!existingManifest.sidecar_bundle_digest ||
      existingManifest.sidecar_bundle_digest !== sidecarBundleDigest(existingConfig)) {
    throw Error('Existing sidecar configuration was changed outside Connect; reconcile it before running Connect again');
  }
  // A normal rerun is allowed to replace a prior, fully generated bundle: the
  // control plane may rotate public trust material or policy expiry metadata.
  void nextConfig;
}

export function prepareEcsMvpArtifacts(root, sidecarConfig, { installationId, domain, operations }) {
  if (!sidecarConfig || sidecarConfig.schema !== 'auteric-sidecar/v1') throw Error('Control plane did not return a valid sidecar bundle');
  const rendered = JSON.stringify(sidecarConfig);
  if (FORBIDDEN.test(rendered)) throw Error('Refusing to write a sidecar bundle containing a secret value');
  if (sidecarConfig.operational?.storage_mode !== 'ephemeral') throw Error('This ECS MVP requires explicit ephemeral sidecar storage');
  const dockerIgnore = join(root, '.dockerignore');
  if (existsSync(dockerIgnore) && readFileSync(dockerIgnore, 'utf8').split(/\r?\n/).some(line => /^\/?auteric\/?$/.test(line.trim()))) {
    throw Error('.dockerignore excludes the tracked auteric deployment bundle');
  }
  const configPath = join(root, 'auteric', 'sidecar.json');
  const manifestPath = join(root, 'auteric', 'deployment.json');
  assertReconciled(configPath, manifestPath, sidecarConfig, installationId, domain);
  const bundleDigest = sidecarBundleDigest(sidecarConfig);
  atomicJSON(configPath, sidecarConfig, 0o644);
  atomicJSON(manifestPath, {
    schema: 'auteric-ecs-mvp/v1', installation_id: installationId, domain,
    storage_mode: 'ephemeral', operations: [...operations].sort(),
    sidecar_config: 'auteric/sidecar.json',
    sidecar_bundle_digest: bundleDigest,
    deployment_constraints: ['desired_count=1', 'deployment_minimum_healthy_percent=0', 'deployment_maximum_percent=100'],
    unsupported_operations: ['complete_checkout'],
  }, 0o644);
  return { artifacts: ['auteric/sidecar.json', 'auteric/deployment.json'], configPath, manifestPath };
}
