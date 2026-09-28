// Independent installation validator. Re-reads the generated tree from disk
// and trusts nothing the generator said: it recomputes every digest from the
// file bytes, re-resolves every import against the real files, checks each
// manifest operation against the composition root, scans adapters for
// identity reads from the request body, and verifies contract digests
// against the locked registry. Any failure is a precise error; the CLI exits
// non-zero. Runnable directly: node src/binding/validate.js [path].
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { MANIFEST_PATH } from './manifest.js';
import { bindingDigest, sha256Hex } from './util.js';
import { loadOperationsRegistry } from '../inventory/operations.js';

const SECRET_KEY = /(secret|token|password|private_?key|credential)/i;

// Identity must come from the verified token (ctx.principal / call.Principal),
// never from the request body.
const FORBIDDEN_IDENTITY = [
  { re: /req\.body\.(principal|buyer|buyer_id|user|user_id|customer|customer_id)\b/, label: 'principal read from req.body' },
  { re: /\bbody\[["'](principal|buyer_id|user_id|customer_id)["']\]/, label: 'identity read from body[...]' },
  { re: /\binput\.(principal|buyer_id|user_id|customer_id)\b/, label: 'principal read from input' },
  { re: /\binput\[["'](principal|buyer_id|user_id|customer_id)["']\]/, label: 'identity read from input[...]' },
];

const JS_EXTENSIONS = ['.ts', '.js', '.tsx', '.jsx', '.mjs', '.cjs'];

function fail(errors, check, message, context = {}) {
  errors.push({ check, message, ...context });
}

function resolveJsFile(root, fromFile, specifier) {
  const base = join(root, dirname(fromFile), specifier);
  const candidates = [base, ...JS_EXTENSIONS.map(extension => base.replace(/\.[^.]+$/, '') + extension), ...JS_EXTENSIONS.map(extension => base + extension), ...JS_EXTENSIONS.map(extension => join(base, 'index' + extension))];
  for (const candidate of candidates) {
    if (existsSync(candidate) && !candidate.endsWith('/')) return candidate;
  }
  return null;
}

function jsExported(content, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`^\\s*export\\s+(?:async\\s+)?(?:class|function|const|let|var)\\s+${escaped}\\b`, 'm').test(content)
    || new RegExp(`^\\s*export\\s*\\{[^}]*\\b${escaped}\\b`, 'm').test(content)
    || new RegExp(`\\bexports\\.${escaped}\\b`).test(content)
    || new RegExp(`module\\.exports\\s*=\\s*\\{[^}]*\\b${escaped}\\b`).test(content);
}

function checkJsAdapter(root, relFile, content, operations, errors) {
  const imports = [];
  for (const match of content.matchAll(/import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+["']([^"']+)["']/g)) {
    imports.push({ names: match[1].split(',').map(name => name.trim().split(/\s+as\s+/).pop()).filter(Boolean), specifier: match[2], kind: 'named' });
  }
  for (const match of content.matchAll(/import\s+(\w+)\s+from\s+["']([^"']+)["']/g)) {
    imports.push({ names: [match[1]], specifier: match[2], kind: 'default' });
  }
  for (const item of imports) {
    if (!item.specifier.startsWith('.')) continue; // package imports are pinned via the manifest dependency list
    const target = resolveJsFile(root, relFile, item.specifier);
    if (!target) {
      fail(errors, 'imports', `${relFile}: import "${item.specifier}" does not resolve to a file`, { file: relFile });
      continue;
    }
    const targetContent = readFileSync(target, 'utf8');
    if (item.kind === 'named') {
      for (const name of item.names) {
        if (!jsExported(targetContent, name)) fail(errors, 'imports', `${relFile}: "${name}" is not exported by ${item.specifier}`, { file: relFile });
      }
    } else {
      // default import followed by `const { A, B } = X;`
      for (const match of content.matchAll(/const\s+\{([^}]+)\}\s*=\s*(\w+)\s*;/g)) {
        if (match[2] !== item.names[0]) continue;
        for (const name of match[1].split(',').map(entry => entry.trim()).filter(Boolean)) {
          if (!jsExported(targetContent, name)) fail(errors, 'imports', `${relFile}: "${name}" is not exported by ${item.specifier}`, { file: relFile });
        }
      }
    }
  }
  for (const operation of operations) {
    const symbol = operation.symbol;
    if (!symbol) continue;
    const servicePath = join(root, symbol.file);
    if (!existsSync(servicePath)) {
      fail(errors, 'imports', `${operation.name}: service file ${symbol.file} referenced by the manifest does not exist`, { operation: operation.name });
      continue;
    }
    const serviceContent = readFileSync(servicePath, 'utf8');
    const method = symbol.method.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (!new RegExp(`\\b${method}\\s*\\(`).test(serviceContent)) {
      fail(errors, 'imports', `${operation.name}: business symbol ${symbol.name}.${symbol.method} not found in ${symbol.file} (adapter references a symbol the service no longer provides)`, { operation: operation.name, file: symbol.file });
    }
    if (!new RegExp(`\\b${method}\\s*\\(`).test(content)) {
      fail(errors, 'behavior', `${operation.name}: adapter ${relFile} does not call the traced symbol ${symbol.name}.${symbol.method}`, { operation: operation.name, file: relFile });
    }
  }
}

const PY_EXTERNAL = /^(auteric_merchant|fastapi|django|pytest|pydantic|typing|typing_extensions|contextlib|__future__|dataclasses|enum|json|os|sys|re|asyncio)\b/;

function checkPyAdapter(root, relFile, content, operations, errors) {
  for (const match of content.matchAll(/^\s*from\s+(\.*)([\w.]+)\s+import\s+(.+)$/gm)) {
    const dots = match[1].length;
    const module = match[2];
    const names = match[3].split(',').map(name => name.trim().split(/\s+as\s+/).pop()).filter(Boolean);
    if (!dots && PY_EXTERNAL.test(module)) continue;
    let modulePath;
    if (dots) {
      const base = dirname(relFile).split('/');
      modulePath = [...base.slice(0, base.length - (dots - 1)), ...module.split('.')].join('/');
    } else {
      modulePath = module.split('.').join('/');
    }
    const file = join(root, modulePath + '.py');
    const packageFile = join(root, modulePath, '__init__.py');
    const target = existsSync(file) ? file : existsSync(packageFile) ? packageFile : null;
    if (!target) {
      fail(errors, 'imports', `${relFile}: module "${module}" does not resolve to an importable file`, { file: relFile });
      continue;
    }
    const targetContent = readFileSync(target, 'utf8');
    for (const name of names) {
      const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      if (!new RegExp(`^(class|def|async def)\\s+${escaped}\\b`, 'm').test(targetContent) && !new RegExp(`^${escaped}\\s*=`, 'm').test(targetContent)) {
        fail(errors, 'imports', `${relFile}: "${name}" is not defined in ${module}`, { file: relFile });
      }
    }
  }
  checkTracedSymbol(root, relFile, content, operations, errors);
}

function checkGoAdapter(root, relFile, content, operations, errors, modulePath) {
  for (const match of content.matchAll(/^\s*(?:(\w+)\s+)?"([\w./-]+)"\s*$/gm)) {
    const specifier = match[2];
    if (!modulePath || !specifier.startsWith(modulePath + '/')) continue; // stdlib and the pinned SDK are external
    const dir = join(root, specifier.slice(modulePath.length + 1));
    if (!existsSync(dir) || !readdirSync(dir).some(name => name.endsWith('.go'))) {
      fail(errors, 'imports', `${relFile}: package "${specifier}" does not resolve to a directory with Go sources`, { file: relFile });
      continue;
    }
    for (const operation of operations) {
      const symbol = operation.symbol;
      if (!symbol || !symbol.file.startsWith(specifier.slice(modulePath.length + 1) + '/')) continue;
      const sources = readdirSync(dir).filter(name => name.endsWith('.go')).map(name => readFileSync(join(dir, name), 'utf8')).join('\n');
      if (!new RegExp(`\\btype\\s+${symbol.name}\\b`).test(sources)) {
        fail(errors, 'imports', `${relFile}: type ${symbol.name} is not declared in package ${specifier}`, { file: relFile, operation: operation.name });
      }
    }
  }
  checkTracedSymbol(root, relFile, content, operations, errors);
}

// Shared per-operation traced-symbol checks (method defined, adapter calls it).
function checkTracedSymbol(root, relFile, content, operations, errors) {
  for (const operation of operations) {
    const symbol = operation.symbol;
    if (!symbol) continue;
    const servicePath = join(root, symbol.file);
    if (!existsSync(servicePath)) {
      fail(errors, 'imports', `${operation.name}: service file ${symbol.file} referenced by the manifest does not exist`, { operation: operation.name });
      continue;
    }
    const serviceContent = readFileSync(servicePath, 'utf8');
    const method = symbol.method.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (!new RegExp(`\\b${method}\\s*\\(`).test(serviceContent)) {
      fail(errors, 'imports', `${operation.name}: business symbol ${symbol.name}.${symbol.method} not found in ${symbol.file} (adapter references a symbol the service no longer provides)`, { operation: operation.name, file: symbol.file });
    }
    if (!new RegExp(`\\b${method}\\s*\\(`).test(content)) {
      fail(errors, 'behavior', `${operation.name}: adapter ${relFile} does not call the traced symbol ${symbol.name}.${symbol.method}`, { operation: operation.name, file: relFile });
    }
  }
}

function goModulePath(root) {
  try {
    return readFileSync(join(root, 'go.mod'), 'utf8').match(/^module\s+(\S+)/m)?.[1] || null;
  } catch { return null; }
}

function scanForbidden(root, files, errors) {
  for (const relFile of files) {
    const full = join(root, relFile);
    if (!existsSync(full)) continue;
    const content = readFileSync(full, 'utf8');
    for (const pattern of FORBIDDEN_IDENTITY) {
      if (pattern.re.test(content)) {
        fail(errors, 'identity', `${relFile}: ${pattern.label} — adapters must take identity from the verified context, never the request body`, { file: relFile });
      }
    }
  }
}

function scanSecrets(value, path, errors) {
  if (Array.isArray(value)) return value.forEach((item, index) => scanSecrets(item, `${path}[${index}]`, errors));
  if (value && typeof value === 'object') {
    for (const [key, entry] of Object.entries(value)) {
      if (SECRET_KEY.test(key)) fail(errors, 'secrets', `manifest key "${path}.${key}" looks like a secret; installation manifests carry no secrets`);
      scanSecrets(entry, `${path}.${key}`, errors);
    }
  }
}

export function validateInstallation(root, options = {}) {
  root = resolve(root);
  const errors = [];
  const manifestFile = join(root, MANIFEST_PATH);
  if (!existsSync(manifestFile)) {
    return { ok: false, errors: [{ check: 'manifest', message: `no installation manifest at ${MANIFEST_PATH}; run \`auteric bind\` first` }], operations: { bound: 0, verified: 0 } };
  }
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
  } catch (error) {
    return { ok: false, errors: [{ check: 'manifest', message: `installation manifest is not valid JSON: ${error.message}` }], operations: { bound: 0, verified: 0 } };
  }
  if (manifest.manifest !== 'auteric-installation/v1') fail(errors, 'manifest', `unexpected manifest version: ${JSON.stringify(manifest.manifest)}`);
  if (manifest.transport !== 'native_http') fail(errors, 'manifest', `unexpected transport: ${JSON.stringify(manifest.transport)} (expected native_http)`);
  if (!manifest.sdk?.name || !manifest.sdk?.version) fail(errors, 'manifest', 'manifest is missing the SDK dependency pin');
  scanSecrets(manifest, 'manifest', errors);

  const registryDir = options.registryDir
    || (manifest.registry?.operations_dir && existsSync(manifest.registry.operations_dir) ? manifest.registry.operations_dir : null)
    || loadOperationsRegistry()?.path;
  if (!registryDir) fail(errors, 'registry', 'locked contracts registry not found; cannot verify contract digests');

  const compositionPath = join(root, manifest.composition_root || '');
  const composition = manifest.composition_root && existsSync(compositionPath) ? readFileSync(compositionPath, 'utf8') : null;
  if (!composition) fail(errors, 'routes', `composition root ${manifest.composition_root} is missing`, { file: manifest.composition_root });

  const language = manifest.sdk?.language;
  const modulePath = language === 'go' ? goModulePath(root) : null;
  const adapterFiles = [];
  const operations = Object.entries(manifest.operations || {}).sort(([a], [b]) => (a < b ? -1 : 1));

  for (const [name, operation] of operations) {
    const relFile = operation.adapter_file;
    const full = join(root, relFile || '');
    if (!relFile || !existsSync(full)) {
      fail(errors, 'files', `${name}: adapter file ${relFile} is missing`, { operation: name, file: relFile });
      continue;
    }
    adapterFiles.push(relFile);
    const bytes = readFileSync(full);
    const digest = bindingDigest(bytes, manifest.dependencies || []);
    if (digest !== operation.binding_digest) {
      fail(errors, 'digests', `${name}: binding digest mismatch for ${relFile} (file changed after the manifest was pinned; rerun \`auteric bind\` to reconcile)`, { operation: name, file: relFile });
    }
    if (registryDir) {
      const registryFile = join(registryDir, `${name}.json`);
      if (!existsSync(registryFile)) {
        fail(errors, 'digests', `${name}: no locked registry record at ${registryFile}`, { operation: name });
      } else {
        const contractDigest = 'sha256:' + sha256Hex(readFileSync(registryFile));
        if (contractDigest !== operation.contract_digest) {
          fail(errors, 'digests', `${name}: contract digest does not match the locked registry record ${name}.json`, { operation: name });
        }
      }
    }
    if (composition && !composition.includes(`"${name}"`) && !composition.includes(`'${name}'`)) {
      fail(errors, 'routes', `${name}: operation has no route in the composition root ${manifest.composition_root}`, { operation: name, file: manifest.composition_root });
    }
    if (operation.revalidation_required) {
      fail(errors, 'revalidation', `${name}: operation is flagged revalidation_required (a previous rerun detected a symbol change; apply the proposed patch and rerun)`, { operation: name });
    }
  }

  // Import and traced-symbol checks run once per adapter file (not per
  // operation), so a file with several operations reports each problem once.
  const byFile = new Map();
  for (const [name, operation] of operations) {
    if (!operation.adapter_file || !existsSync(join(root, operation.adapter_file))) continue;
    if (!byFile.has(operation.adapter_file)) byFile.set(operation.adapter_file, []);
    byFile.get(operation.adapter_file).push({ name, symbol: operation.symbol || null });
  }
  for (const [relFile, fileOps] of [...byFile.entries()].sort()) {
    const content = readFileSync(join(root, relFile), 'utf8');
    if (language === 'python') checkPyAdapter(root, relFile, content, fileOps, errors);
    else if (language === 'go') checkGoAdapter(root, relFile, content, fileOps, errors, modulePath);
    else checkJsAdapter(root, relFile, content, fileOps, errors);
  }

  scanForbidden(root, [...adapterFiles, manifest.composition_root].filter(Boolean), errors);

  const globalError = errors.some(error => !error.operation && (!error.file || error.file === manifest.composition_root));
  const verified = globalError ? 0 : operations.filter(([name, operation]) =>
    !errors.some(error => error.operation === name || (error.file && error.file === operation.adapter_file))).length;

  return { ok: errors.length === 0, errors, operations: { bound: operations.length, verified } };
}

if (process.argv[1] && existsSync(process.argv[1]) && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const target = resolve(process.argv[2] || '.');
  const result = validateInstallation(target);
  for (const error of result.errors) console.error(`${error.check}: ${error.message}`);
  console.log(result.ok ? `Validation passed: ${result.operations.verified}/${result.operations.bound} operations verified` : `Validation failed: ${result.errors.length} problem(s), ${result.operations.verified}/${result.operations.bound} operations verified`);
  process.exitCode = result.ok ? 0 : 1;
}
