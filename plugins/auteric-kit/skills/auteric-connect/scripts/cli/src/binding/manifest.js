// Installation manifest: .auteric/installation.json. Records every bound
// operation with its contract digest (sha256 of the locked registry record),
// its binding digest (sha256 over the on-disk adapter bytes plus the
// dependency list), and the SDK dependency pin. The manifest carries no
// secrets, no timestamps and no environment-specific state, so identical
// inputs produce byte-identical manifests and clean reruns write nothing.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { bindingDigest, sha256Hex } from './util.js';

export const MANIFEST_PATH = join('.auteric', 'installation.json');

const EMBEDDED_PINS = {
  node: { name: '@auteric/merchant-node', version: '0.1.0' },
  python: { name: 'auteric-merchant', version: '0.1.0' },
  go: { name: 'github.com/auteric-ai/merchant-go', version: 'v0.1.0' },
};

// Locates the monorepo packages/ directory by walking up from this file;
// standalone installs fall back to the embedded pins above.
function packagesDir() {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 8; depth++) {
    if (existsSync(join(dir, 'packages', 'merchant-node', 'package.json'))) return join(dir, 'packages');
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

export function sdkPin(language) {
  const fallback = { language, ...EMBEDDED_PINS[language] };
  const packages = packagesDir();
  if (!packages) return fallback;
  try {
    if (language === 'node') {
      const data = JSON.parse(readFileSync(join(packages, 'merchant-node', 'package.json'), 'utf8'));
      return { language, name: data.name, version: data.version };
    }
    if (language === 'python') {
      const text = readFileSync(join(packages, 'merchant-python', 'pyproject.toml'), 'utf8');
      return {
        language,
        name: text.match(/^name\s*=\s*"([^"]+)"/m)?.[1] || fallback.name,
        version: text.match(/^version\s*=\s*"([^"]+)"/m)?.[1] || fallback.version,
      };
    }
    if (language === 'go') {
      const text = readFileSync(join(packages, 'merchant-go', 'go.mod'), 'utf8');
      return { language, name: text.match(/^module\s+(\S+)/m)?.[1] || fallback.name, version: fallback.version };
    }
  } catch { /* fall through to the embedded pin */ }
  return fallback;
}

// Framework dependency the composition root imports, pinned from the target
// repo's own manifest when discoverable.
function frameworkPin(language, root) {
  try {
    if (language === 'node') {
      const data = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
      const deps = { ...data.dependencies, ...data.devDependencies };
      for (const name of ['express', 'fastify', 'next']) if (deps[name]) return `${name}@${deps[name]}`;
    }
    if (language === 'python') {
      const text = readFileSync(join(root, 'requirements.txt'), 'utf8');
      const line = text.split('\n').map(entry => entry.trim()).find(entry => /^(fastapi|django)/i.test(entry));
      if (line) return line;
    }
  } catch { /* no framework pin available */ }
  return null;
}

export function readManifest(root) {
  try {
    return JSON.parse(readFileSync(join(root, MANIFEST_PATH), 'utf8'));
  } catch { return null; }
}

// Builds the manifest from the plan plus the post-reconcile on-disk state.
// reconcileActions: output of reconcile(); used to mark manual_edit_preserved
// and revalidation_required entries honestly.
export function buildManifest(plan, options) {
  const { root, reconcileActions = [] } = options;
  const sdk = sdkPin(plan.backend.language);
  const dependencies = [`${sdk.name}@${sdk.version}`];
  const framework = frameworkPin(plan.backend.language, root);
  if (framework) dependencies.push(framework);

  const preserved = new Set();
  for (const action of reconcileActions) {
    if (action.action !== 'manual_edit_preserved') continue;
    for (const operation of action.operations || []) preserved.add(operation);
  }
  const drifted = new Map();
  for (const action of reconcileActions) {
    if (action.action !== 'symbol_drift_preserved') continue;
    for (const operation of action.operations || []) drifted.set(operation, action.drift);
  }

  const operations = {};
  for (const binding of plan.bindings) {
    const adapterPath = join(root, binding.adapter_file);
    if (!existsSync(adapterPath)) throw Error(`adapter file missing after generation: ${binding.adapter_file}`);
    const adapterBytes = readFileSync(adapterPath);
    const drift = drifted.get(binding.operation);
    operations[binding.operation] = {
      action: binding.action,
      adapter_file: binding.adapter_file,
      merchant_http: { method: binding.contract.method, path: binding.contract.path },
      contract_version: binding.contract.contract_version,
      contract_digest: 'sha256:' + sha256Hex(readFileSync(join(plan.registry.path, `${binding.operation}.json`))),
      binding_digest: bindingDigest(adapterBytes, dependencies),
      source_digest: binding.source_digest,
      // On symbol drift the adapter still references the previous symbol;
      // record that one so the validator checks the adapter against reality.
      symbol: drift ? drift.from : binding.symbol,
      ...(preserved.has(binding.operation) ? { manual_edit_preserved: true } : {}),
      ...(drift ? { revalidation_required: true } : {}),
    };
  }

  return {
    manifest: 'auteric-installation/v1',
    transport: 'native_http',
    sdk,
    dependencies,
    registry: { source: 'contracts', operations_dir: plan.registry.path },
    composition_root: plan.composition_root,
    operations,
    decisions: plan.decisions.map(entry => ({
      operation: entry.operation,
      decision: entry.decision,
      status: 'requires_merchant_decision',
      ...(entry.evidence ? { evidence: entry.evidence } : {}),
    })),
  };
}

// Writes the manifest only when its bytes change; returns whether it wrote.
export function writeManifestIfChanged(root, manifest) {
  const path = join(root, MANIFEST_PATH);
  const contents = JSON.stringify(manifest, null, 2) + '\n';
  if (existsSync(path) && readFileSync(path, 'utf8') === contents) return false;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, contents, { mode: 0o644 });
  return true;
}
