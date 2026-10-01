import { prepareWithAgent } from './agent.js';
import { atomicJSON, journal, lockProject, sessionPath, cachedSession, projectDigest, readJSON, connectionStatus } from './workflow.js';
import { sdk } from './sdk.js';
import { inventoryRepo } from './inventory/index.js';
import { bindRepo, bindingSummaryLines, normalizeStorePlatform, platformAdapter, validateInstallation } from './binding/index.js';
import { loadOperationsRegistry } from './inventory/operations.js';
import { runAcceptance, acceptanceSummaryLines } from './acceptance/index.js';
import {
  CUSTOM_MVP_OPERATIONS, prepareServiceFirstBundle, serviceFirstBindingDigest,
  serviceFirstOperationEvidence, serviceFirstSupport, verifyStandardMerchantBridge,
} from './sidecar/bundle.js';
import { cliProgress, fraction, terminalColor } from './progress.js';
import { prepareHTTP, prepareResult } from './connect/prepare.js';
import { shared } from './connect/shared.js';
import { MANAGED_STATE, stateDirectory } from './connect/layout.js';
import { initializeManaged } from './connect/artifacts.js';
import { disconnectHTTP } from './connect/disconnect.js';
import { integrationDossier, installModule, disconnectModule } from './connect/module.js';
import { localAcceptance } from './connect/local-acceptance.js';
import { validateRegistration } from './connect/control-contract.js';
import { createHash, randomBytes } from 'node:crypto';
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync, unlinkSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const PROD_API = 'https://control.auteric.com';
const LOCAL_API = 'http://127.0.0.1:8100';
// The first native reference is deliberately small.  Keeping this allowlist
// here prevents an inventory result (or a stale connector) from silently
// widening the production installation.
export const NATIVE_PHASE1_OPERATIONS = Object.freeze([
  'search_products', 'get_product', 'create_cart', 'get_cart',
  'add_to_cart', 'update_cart_item', 'remove_from_cart',
]);

function failureDetails(error) {
  const message = String(error?.message || 'Unknown connection failure')
    .replace(/Bearer\s+[^\s]+/gi, 'Bearer [redacted]')
    .replace(/([?&](?:token|secret|credential)=)[^&\s]+/gi, '$1[redacted]')
    .slice(0, 500);
  if (/timed out|timeout|abort/i.test(message)) return { failure_code: 'timeout', message, next_action: 'Check the control-plane and storefront health, then retry. The previous attempt is terminal.' };
  if (/^404\b/.test(message)) return { failure_code: 'control_plane_route_missing', message, next_action: 'The requested Auteric control-plane route is unavailable. Repair the service route before retrying.' };
  if (/Cannot contact/.test(message)) return { failure_code: 'control_plane_unreachable', message, next_action: 'Check the configured Auteric control-plane URL and its health.' };
  if (/authorization|sign in|401|403/i.test(message)) return { failure_code: 'authentication_failed', message, next_action: 'Start a new pairing attempt and complete browser authorization before it expires.' };
  return { failure_code: 'connection_failed', message, next_action: 'Run auteric connect-status and read the phase-specific report before retrying.' };
}

function recordConnection(root, phase, status, extra = {}, directory = stateDirectory(root)) {
  journal(root, phase, status, {}, directory);
  return connectionStatus(root, { phase, status, ...extra }, directory);
}

function parse(argv) {
  // `npx github:auteric-ai/auteric-kit --localhost ...` invokes the package's
  // only binary with flags as argv. Treat that form as Connect so merchants do
  // not have to learn a second, package-specific subcommand.
  const [first = 'help', ...rest] = argv;
  const command = first.startsWith('--') ? 'connect' : first;
  const tail = first.startsWith('--') ? [first, ...rest] : rest;
  const options = {};
  for (let i = 0; i < tail.length; i++) {
    const arg = tail[i];
    if (!arg.startsWith('--')) {
      if (['inventory', 'bind', 'validate', 'verify'].includes(command) && options._path === undefined) { options._path = arg; continue; }
      throw Error(`Unknown argument: ${arg}`);
    }
    const [name, value] = arg.slice(2).split('=', 2);
    if (['localhost', 'local-storefront', 'approve-publication', 'approve-adapters', 'dry-run', 'yes', 'no-browser', 'serve', 'no-agent', 'skip-project-checks', 'quiet', 'json', 'legacy-connector', 'local-acceptance'].includes(name)) {
      options[name] = true;
    } else if (['api-url', 'domain', 'agent', 'store-url', 'backend', 'frontend', 'backend-url', 'port', 'platform', 'approved-by', 'mapping', 'deployment', 'environment', 'test-origin', 'test-query', 'test-currency', 'runtime-source', 'runtime-commit', 'runtime-image', 'python', 'adapter-plan'].includes(name)) {
      options[name] = value ?? tail[++i];
      if (!options[name]) throw Error(`--${name} needs a value`);
    } else throw Error(`Unknown flag: --${name}`);
  }
  return { command, options };
}

export function apiUrl(options = {}) {
  const raw = options['api-url'] || (options.localhost ? LOCAL_API : PROD_API);
  const url = new URL(raw);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('API URL must contain only an origin');
  if (options.localhost) {
    if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
      throw Error('--localhost permits only a plain HTTP loopback server');
    }
  } else if (url.protocol !== 'https:') throw Error('Cloud API requires HTTPS; use --localhost for local testing');
  return url.origin;
}

export function localStoreUrl(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw Error('--store-url must be a plain HTTP loopback origin, for example http://127.0.0.1:5500');
  }
  if (url.hostname === 'localhost') url.hostname = '127.0.0.1';
  return url.origin;
}

export function validateConnectOptions(options = {}) {
  const localSession = Boolean(options.localhost || options['local-storefront']);
  if (options['store-url'] && !localSession)
    throw Error('--store-url is local-only. Remove it for a public store and use --domain store.example.com.');
  return localSession;
}

const LOCAL_STOREFRONT_PORTS = [9020, 5173, 3000, 3001, 8080, 8000, 5500];

/** Find one running merchant origin without accepting any non-loopback host. */
export async function detectLocalStoreUrl(ports = LOCAL_STOREFRONT_PORTS) {
  for (const port of ports) {
    const origin = `http://127.0.0.1:${port}`;
    for (const path of ['/api/health', '/health', '/']) {
      try {
        const response = await fetch(origin + path, {
          redirect: 'error', signal: AbortSignal.timeout(750),
        });
        if (response.ok) return origin;
      } catch { /* This port/path is not a running local storefront. */ }
    }
  }
  throw Error('No running loopback storefront was detected. Start the merchant service, or supply --store-url http://127.0.0.1:PORT.');
}

export function inspect(root) {
  const isFile = file => existsSync(join(root, file));
  const packageData = isFile('package.json') ? JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')) : {};
  const deps = { ...packageData.dependencies, ...packageData.devDependencies };
  const framework = deps.next ? 'next' : deps.nuxt ? 'nuxt' : deps.vite ? 'vite' : deps.express ? 'express' : isFile('index.html') ? 'static' : 'custom';
  const agents = ['codex', 'claude', 'cursor'].filter(agent => isFile(agent === 'cursor' ? '.cursor' : agent === 'claude' ? '.claude' : '.codex') || executable(agent === 'cursor' ? 'cursor-agent' : agent));
  return { framework, agents, catalogCandidate: Object.keys(deps).filter(dep => /commerce|shopify|medusa|stripe/.test(dep)),
    existingUcp: [join(root, '.well-known/ucp'), join(root, 'public/.well-known/ucp')].filter(existsSync) };
}

function packageDependencies(root) {
  try {
    const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
    return { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) };
  } catch { return {}; }
}

/**
 * Detect a merchant-native installation from production source, never from a
 * connector configuration.  A stale `.auteric/connector.json` is therefore
 * unable to select the outbound-worker path for a native merchant.
 */
export function nativeReference(root) {
  const runtime = join(root, 'server', 'auteric', 'runtime.js');
  const app = join(root, 'server', 'app.js');
  const reasons = [];
  if (!existsSync(runtime)) reasons.push('missing server/auteric/runtime.js');
  if (!packageDependencies(root)['@auteric/merchant-node']) reasons.push('package.json does not declare @auteric/merchant-node');
  let appSource = '';
  try { appSource = readFileSync(app, 'utf8'); } catch { reasons.push('missing server/app.js'); }
  if (appSource && !/app\.use\(\s*['"]\/api\/auteric\/v1['"]/.test(appSource)) {
    reasons.push('Express app does not mount /api/auteric/v1');
  }
  return {
    detected: reasons.length === 0,
    transport: reasons.length === 0 ? 'native_http' : null,
    runtime_file: runtime,
    reasons,
  };
}

export function nativeBindingDigest(root) {
  const source = readFileSync(join(root, 'server', 'auteric', 'runtime.js'));
  return 'sha256:' + createHash('sha256')
    .update('auteric-native-phase1/v1\0')
    .update(source)
    .digest('hex');
}

function nativeOperationEvidence() {
  const registry = loadOperationsRegistry();
  if (!registry) throw Error('Locked commerce contracts registry is required for a native installation');
  const indexPath = join(registry.path, '..', 'registry.json');
  if (!existsSync(indexPath)) throw Error('Locked commerce contracts registry index is required for a native installation');
  const canonical = value => {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
    return JSON.stringify(value);
  };
  // Capability controls compare against the signed, whole-registry digest.
  // Per-operation file hashes are useful inventory metadata, but are not the
  // compatibility authority for an installed native runtime.
  const contractDigest = 'sha256:' + createHash('sha256').update(canonical(JSON.parse(readFileSync(indexPath, 'utf8')))).digest('hex');
  return NATIVE_PHASE1_OPERATIONS.map(operation => {
    const record = registry.operations[operation];
    if (!record) throw Error(`Locked commerce contract is missing ${operation}`);
    return {
      operation,
      contract_digest: contractDigest,
      binding_digest: null, // assigned only after the runtime source is checked
      test_evidence: { kind: 'merchant_project_validation', status: 'pass' },
      deployment_evidence: { kind: 'native_reference_source', status: 'checked' },
    };
  });
}

const DISCOVERY_SKIP = new Set(['.git', '.auteric', 'node_modules', 'vendor', 'dist', 'build', 'coverage', '__pycache__']);

function packageAt(directory) {
  const path = join(directory, 'package.json');
  try { return JSON.parse(readFileSync(path, 'utf8')); } catch { return null; }
}

function isDirectory(path) {
  try { return lstatSync(path).isDirectory(); } catch { return false; }
}

function contains(root, candidate) {
  const value = relative(root, candidate);
  const parentPrefix = '..' + (process.platform === 'win32' ? '\\' : '/');
  return value === '' || (!value.startsWith('..') && !value.startsWith(parentPrefix));
}

function candidateKind(directory) {
  const pkg = packageAt(directory);
  const deps = { ...(pkg?.dependencies || {}), ...(pkg?.devDependencies || {}) };
  const names = Object.keys(deps);
  const nodeApi = names.some(name => /^(express|fastify|koa|hono|@nestjs\/core|@nestjs\/platform)/.test(name));
  const nodeUi = names.some(name => /^(next|nuxt|react|vue|@angular\/core|svelte)/.test(name));
  let files = [];
  try { files = readdirSync(directory, { withFileTypes: true }); } catch { return { api: false, frontend: false }; }
  const hasStatic = files.some(file => file.isFile() && file.name === 'index.html');
  const hasPythonApi = files.some(file => file.isFile() && /^(main|app|server)\.py$/.test(file.name));
  const namesInDirectory = new Set(files.filter(file => file.isFile()).map(file => file.name));
  const hasBackendManifest = ['composer.json', 'Gemfile', 'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts']
    .some(name => namesInDirectory.has(name)) || files.some(file => file.isFile() && file.name.endsWith('.csproj'));
  const hasBackendSource = files.some(file => file.isFile() && /^(routes|router|server|main|app)\.(php|rb|go|java|kt|cs)$/.test(file.name));
  return { api: nodeApi || hasPythonApi || hasBackendManifest || hasBackendSource, frontend: nodeUi || hasStatic };
}

function walkProject(root, maximumDepth = 4) {
  const found = [];
  const visit = (directory, depth) => {
    if (lstatSync(directory).isSymbolicLink()) return;
    const kind = candidateKind(directory);
    if (kind.api || kind.frontend) found.push({ path: directory, ...kind });
    if (depth === maximumDepth) return;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isDirectory() || DISCOVERY_SKIP.has(entry.name)) continue;
      const child = join(directory, entry.name);
      try { if (!lstatSync(child).isSymbolicLink()) visit(child, depth + 1); } catch { /* unreadable child is ignored */ }
    }
  };
  visit(root, 0);
  return found;
}

function selectDirectory(root, supplied, candidates, label) {
  if (supplied) {
    const selected = resolve(root, supplied);
    if (!contains(root, selected) || !isDirectory(selected) || lstatSync(selected).isSymbolicLink())
      throw Error(`--${label} must name a non-symlink directory inside the project root`);
    return selected;
  }
  const unique = [...new Set(candidates.map(candidate => candidate.path))];
  if (unique.length === 1) return unique[0];
  if (unique.length === 0) return root;
  const choices = unique.map(path => relative(root, path) || '.').join(', ');
  throw Error(`Found multiple ${label} candidates: ${choices}. Re-run with --${label} <relative-directory>.`);
}

export function resolveProjectLayout(root, options = {}) {
  const candidates = walkProject(root);
  const backend = selectDirectory(root, options.backend, candidates.filter(candidate => candidate.api), 'backend');
  const frontend = selectDirectory(root, options.frontend, candidates.filter(candidate => candidate.frontend), 'frontend');
  return {
    root,
    backend,
    frontend,
    backendRelative: relative(root, backend) || '.',
    frontendRelative: relative(root, frontend) || '.',
    candidates: candidates.map(candidate => ({ path: relative(root, candidate.path) || '.', api: candidate.api, frontend: candidate.frontend })),
  };
}

function executable(command) {
  return (process.env.PATH || '').split(process.platform === 'win32' ? ';' : ':').some(dir => existsSync(join(dir, command)));
}

function openBrowser(url) {
  const cmd = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'cmd' : 'xdg-open';
  const args = process.platform === 'win32' ? ['/c', 'start', '', url] : [url];
  const child = spawn(cmd, args, { stdio: 'ignore', detached: true });
  child.on('error', () => {});
  child.unref();
}

async function request(base, path, { method = 'GET', body, token } = {}) {
  if(method==='POST' && /^\/api\/commerce\/stores\/[^/]+\/installations$/.test(path))validateRegistration(body);
  let response;
  try {
    response = await fetch(base + path, { method, headers: {
      ...(body ? { 'content-type': 'application/json' } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(method !== 'GET' && token ? { 'x-auteric-console': '1' } : {}),
    }, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(12000), redirect: 'error' });
  } catch { throw Error(`Cannot contact ${base}. Start the Commerce service or check its URL.`); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(`${response.status} ${serviceErrorMessage(data)}`);
  return data;
}

export function serviceErrorMessage(data = {}) {
  if (typeof data.detail === 'string' && data.detail) return data.detail;
  if (Array.isArray(data.detail)) {
    const detail = data.detail
      .map(issue => {
        const location = Array.isArray(issue?.loc) ? issue.loc.join('.') : '';
        return [location, issue?.msg].filter(Boolean).join(': ');
      })
      .filter(Boolean)
      .join('; ');
    if (detail) return detail;
  }
  return 'Service request failed';
}

function activityEnabled(options = {}) {
  return Boolean(process.stdout.isTTY && !options.quiet && !options.json);
}

async function activity(label, work, options = {}) {
  if (!activityEnabled(options)) return work();
  let dots = 1;
  const started = Date.now();
  const render = () => process.stdout.write(`\r${label} ${'.'.repeat(dots)}${' '.repeat(3 - dots)}`);
  render();
  const timer = setInterval(() => { dots = dots === 3 ? 1 : dots + 1; render(); }, 400);
  try {
    const value = await work();
    clearInterval(timer);
    process.stdout.write(`${terminalColor(`\r${label} ✓ (${((Date.now() - started) / 1000).toFixed(1)}s)`, 'green', { enabled: activityEnabled(options) })}\n`);
    return value;
  } catch (error) {
    clearInterval(timer);
    process.stdout.write(`${terminalColor(`\r${label} ✗ (${((Date.now() - started) / 1000).toFixed(1)}s)`, 'red', { enabled: activityEnabled(options) })}\n`);
    throw error;
  }
}

function runProcess(command, args, cwd) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    child.stdout.on('data', data => { output += data; });
    child.stderr.on('data', data => { output += data; });
    child.on('error', error => reject(error));
    child.on('close', code => {
      if (code === 0) resolvePromise();
      else reject(Error(`${command} ${args.join(' ')} failed in ${cwd}: ${output.slice(-1200)}`));
    });
  });
}

async function runProjectValidation(layout, options) {
  const targets = [...new Set([layout.backend, layout.frontend])];
  const checks = [];
  for (const directory of targets) {
    const manifest = join(directory, 'package.json');
    if (!existsSync(manifest)) continue;
    const packageJson = JSON.parse(readFileSync(manifest, 'utf8'));
    const scripts = packageJson.scripts || {};
    if (!existsSync(join(directory, 'node_modules')) && existsSync(join(directory, 'package-lock.json'))) {
      await activity(`Installing locked Node dependencies (${relative(process.cwd(), directory) || '.'})`,
        () => runProcess('npm', ['ci'], directory), options);
      checks.push({ directory: relative(process.cwd(), directory) || '.', command: 'npm ci', status: 'passed' });
    }
    // The monorepo reference deliberately consumes the merchant SDK through a
    // local file dependency. npm links it but does not build its ignored dist/
    // output, so build it before the merchant test imports it. Published SDKs
    // have their dist/ artifact already and do not enter this branch.
    const merchantSdk = packageJson.dependencies?.['@auteric/merchant-node'];
    if (typeof merchantSdk === 'string' && merchantSdk.startsWith('file:')) {
      const sdkDirectory = resolve(directory, merchantSdk.slice('file:'.length));
      const sdkManifest = join(sdkDirectory, 'package.json');
      if (!existsSync(sdkManifest)) throw Error(`Local @auteric/merchant-node dependency is missing: ${sdkDirectory}`);
      const sdkScripts = JSON.parse(readFileSync(sdkManifest, 'utf8')).scripts || {};
      if (!sdkScripts.build) throw Error('Local @auteric/merchant-node dependency has no build script');
      if (!existsSync(join(sdkDirectory, 'node_modules'))) {
        const sdkLock = join(sdkDirectory, 'package-lock.json');
        if (!existsSync(sdkLock)) throw Error('Local @auteric/merchant-node dependency needs a package-lock.json for reproducible build');
        await activity('Installing locked local @auteric/merchant-node dependencies', () => runProcess('npm', ['ci'], sdkDirectory), options);
        checks.push({ directory: relative(process.cwd(), sdkDirectory) || '.', command: 'npm ci (@auteric/merchant-node)', status: 'passed' });
      }
      await activity('Building local @auteric/merchant-node SDK', () => runProcess('npm', ['run', 'build'], sdkDirectory), options);
      checks.push({ directory: relative(process.cwd(), sdkDirectory) || '.', command: 'npm run build (@auteric/merchant-node)', status: 'passed' });
    }
    for (const script of ['test', 'build']) {
      if (!scripts[script]) continue;
      await activity(`Running npm ${script} (${relative(process.cwd(), directory) || '.'})`,
        () => runProcess('npm', ['run', script], directory), options);
      checks.push({ directory: relative(process.cwd(), directory) || '.', command: `npm run ${script}`, status: 'passed' });
    }
  }
  const result = { status: 'passed', checked_at: new Date().toISOString(), checks };
  atomicJSON(join(layout.backend, '.auteric/project-validation.json'), result);
  return result;
}

export async function probeLocalDiscovery(storeUrl, document) {
  const target = localStoreUrl(storeUrl) + '/.well-known/ucp';
  let response;
  try {
    response = await fetch(target, { redirect: 'error', signal: AbortSignal.timeout(5000) });
  } catch {
    return { available: false, reason: `Cannot fetch ${target}. Start the storefront and expose the generated UCP file.` };
  }
  if (response.status !== 200) return { available: false, reason: `${target} returned HTTP ${response.status}. Check the storefront's public/static directory.` };
  if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') || ''))
    return { available: false, reason: `${target} must return Content-Type: application/json (the server may be serving a static file or SPA fallback).` };
  let observed;
  try { observed = await response.json(); } catch { return { available: false, reason: `${target} did not return JSON.` }; }
  if (JSON.stringify(observed) !== JSON.stringify(document))
    return { available: false, reason: `${target} serves a different UCP document. Check dev-server caching or the public/static directory.` };
  return { available: true, url: target };
}

export async function verifyLocalWithRetry(base, storeId, token, storeUrl, document, attempts = 4, pause = delay) {
  let probe;
  for (let attempt = 0; attempt < attempts; attempt++) {
    probe = await probeLocalDiscovery(storeUrl, document);
    if (probe.available) break;
    if (attempt < attempts - 1) await pause(500 * (attempt + 1));
  }
  if (!probe.available) return { local_verified: false, reason: probe.reason };
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await request(base, `/api/commerce/stores/${encodeURIComponent(storeId)}/verify-local`, {
        method: 'POST', token, body: { store_url: storeUrl },
      });
    } catch (error) {
      if (!/^422 Local UCP route is unavailable or invalid$/.test(error.message)) throw error;
      if (attempt < attempts - 1) await pause(500 * (attempt + 1));
    }
  }
  return { local_verified: false, reason: `Auteric could not fetch ${probe.url} after ${attempts} attempts. The signed file and tested connector are saved; check that the Commerce service can reach this loopback port.` };
}

async function authenticate(base, options) {
  const verifier = randomBytes(32).toString('base64url');
  const state = randomBytes(32).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  const approvalMode = options['no-browser'] ? 'device' : 'browser';
  const session = await request(base, '/api/commerce/cli/start', {
    method: 'POST', body: { challenge, state, approval_mode: approvalMode },
  });
  const link = new URL(session.authorization_url);
  if (link.origin !== base || link.pathname !== '/cli/authorize') throw Error('Unexpected authorization URL from control plane');
  console.log(`${options['no-browser'] ? 'Open' : 'Opening'} this Auteric sign-in page:\n${link.href}`);
  if (approvalMode === 'device') {
    if (!/^\d{4}-\d{4}$/.test(session.user_code)) throw Error('Invalid pairing code from control plane');
    console.log(`Merchant ID (one-time pairing code): ${session.user_code}`);
  }
  if (!options['no-browser']) openBrowser(link.href);
  const deadline = Math.min(session.expires_at * 1000, Date.now() + 300000);
  return activity('Waiting for browser authorization', async () => {
    while (Date.now() < deadline) {
      await delay(Math.max(2000, Number(session.interval || 2) * 1000));
      const reply = await request(base, '/api/commerce/cli/poll', {
        method: 'POST', body: { request_id: session.request_id, state, verifier },
      });
      if (reply.status === 'authorized') return { ...reply, request_id: session.request_id };
    }
    throw Error('Browser authorization expired. Run connect again.');
  }, options);
}

function domainName(input) {
  if (!input) throw Error('Supply --domain store.example.com (the merchant domain to verify)');
  const domain = input.replace(/^https?:\/\//, '').replace(/\/$/, '').toLowerCase();
  if (!/^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(domain)) throw Error('Expected a public merchant hostname');
  return domain;
}

export function localTestDomain(root) {
  const previous = readConfig(root);
  if (previous?.local_only && /^local-[a-f0-9]{12}\.auteric\.test$/.test(previous.domain || '')) {
    return previous.domain;
  }
  return `local-${createHash('sha256').update(resolve(root)).digest('hex').slice(0, 12)}.auteric.test`;
}

function destination(root, framework) {
  return join(root, ['next', 'nuxt', 'vite'].includes(framework) || (framework !== 'static' && existsSync(join(root, 'public'))) ? 'public/.well-known/ucp' : '.well-known/ucp');
}

export function prepareDiscovery(root, framework, document, options = {}) {
  if (!document?.ucp?.version || !document.auteric_attestation?.signature) throw Error('Control plane did not return a signed UCP profile');
  const path = destination(root, framework);
  const contents = JSON.stringify(document, null, 2) + '\n';
  const relativeParts = path.slice(root.length + 1).split('/');
  let part = root;
  for (const segment of relativeParts) {
    part = join(part, segment);
    if (existsSync(part) && lstatSync(part).isSymbolicLink()) throw Error(`Refusing to follow a symlink: ${part}`);
  }
  if (existsSync(path)) {
    if (readFileSync(path, 'utf8') !== contents) {
      const previous = readFileSync(path, 'utf8');
      if (!options.previousDigest || createHash('sha256').update(previous).digest('hex') !== options.previousDigest)
        throw Error(`Existing UCP profile at ${path} differs. Reconcile it; no file was overwritten.`);
      if (!options['dry-run']) writeFileSync(path, contents, { mode: 0o644 });
      return { path, changed: true };
    }
    return { path, changed: false };
  }
  if (!options['dry-run']) {
    mkdirSync(resolve(path, '..'), { recursive: true });
    writeFileSync(path, contents, { flag: 'wx', mode: 0o644 });
  }
  return { path, changed: true };
}

export function prepareBuiltDiscovery(frontend, document, previousDigest) {
  // Express/static production previews commonly serve dist rather than Vite's
  // public directory. Keep the built copy in sync without rebuilding code.
  const built = join(frontend, 'dist');
  if (!existsSync(join(built, 'index.html'))) return null;
  return prepareDiscovery(built, 'static', document, { previousDigest });
}

function configPath(root) { return join(root, stateDirectory(root), 'config.json'); }
function readConfig(root) { try { return JSON.parse(readFileSync(configPath(root), 'utf8')); } catch { return null; } }

export function uncoveredOperations(inventory, preparedOperations = []) {
  const prepared = new Set(preparedOperations);
  return (inventory.capability_coverage || [])
    .filter(item => item.status === 'candidate' && !prepared.has(item.operation))
    .map(item => item.operation);
}

export function existingDiscoveryDigest(root, framework, storeId) {
  const path = destination(root, framework);
  try {
    const contents = readFileSync(path, 'utf8');
    const profile = JSON.parse(contents);
    // Recover only a previous profile for this exact Store. A profile belonging
    // to another Store remains protected from accidental replacement.
    if (profile?.auteric_attestation?.payload?.store_id !== storeId) return null;
    return createHash('sha256').update(contents).digest('hex');
  } catch { return null; }
}

async function provisionGatewayAccess(root, base, domain, storeId, token, operations, signedEndpoint) {
  const scopes = operations.filter(operation => ['search_products', 'get_product', 'get_cart', 'get_checkout'].includes(operation));
  if (!scopes.length || !signedEndpoint) return null;
  const secretFile = sessionPath(root, base, domain).replace(/\.json$/, '.gateway.json');
  if (existsSync(secretFile) && (lstatSync(secretFile).mode & 0o077))
    throw Error('Gateway credential file must be private to the current user');
  const saved = readJSON(secretFile);
  const listing = await request(base, `/api/commerce/stores/${encodeURIComponent(storeId)}/mcp-credentials`, { token });
  if (listing.mcp_url !== signedEndpoint) throw Error('Gateway URL differs from the signed UCP profile');
  const valid = saved?.store_id === storeId && saved?.mcp_url === signedEndpoint &&
    scopes.every(scope => saved.operations?.includes(scope)) &&
    listing.credentials?.some(item => item.credential_id === saved.credential_id && !item.revoked && scopes.every(scope => item.operations.includes(scope)));
  if (valid) return { credential_file: secretFile, credential_id: saved.credential_id, operations: scopes };
  const granted = await request(base, `/api/commerce/stores/${encodeURIComponent(storeId)}/mcp-credentials`, {
    method: 'POST', token, body: { operations: scopes },
  });
  if (granted.mcp_url !== signedEndpoint) {
    await request(base, `/api/commerce/stores/${encodeURIComponent(storeId)}/mcp-credentials/${encodeURIComponent(granted.credential_id)}`, { method: 'DELETE', token });
    throw Error('New Gateway grant differs from signed UCP profile');
  }
  try {
    atomicJSON(secretFile, { store_id: storeId, mcp_url: granted.mcp_url, credential_id: granted.credential_id,
      operations: scopes, token: granted.token });
  } catch (error) {
    await request(base, `/api/commerce/stores/${encodeURIComponent(storeId)}/mcp-credentials/${encodeURIComponent(granted.credential_id)}`, { method: 'DELETE', token });
    throw error;
  }
  if (saved?.store_id === storeId && saved.credential_id !== granted.credential_id &&
      listing.credentials?.some(item => item.credential_id === saved.credential_id && !item.revoked)) {
    await request(base, `/api/commerce/stores/${encodeURIComponent(storeId)}/mcp-credentials/${encodeURIComponent(saved.credential_id)}`,
      { method: 'DELETE', token });
  }
  return { credential_file: secretFile, credential_id: granted.credential_id, operations: scopes };
}

function nativeRuntimePath(root) { return join(root, '.auteric', 'native-runtime.json'); }
function nativeInstallationPath(root) { return join(root, '.auteric', 'native-installation.json'); }

function assertNativeRuntimeConfig(config, { storeId, environment, endpoint, bindingDigest }) {
  if (!config || config.config_version !== 'auteric-native-runtime/v1') {
    throw Error('Control plane returned an invalid native runtime configuration');
  }
  if (config.endpoint !== endpoint || config.installation?.storeId !== storeId ||
      config.installation?.environment !== environment || config.installation?.bindingDigest !== bindingDigest ||
      !config.installation?.installationId || config.installation?.enabled !== true) {
    throw Error('Native runtime configuration does not match the registered Store, environment, endpoint, or binding digest');
  }
  if (!Array.isArray(config.installation.operations) ||
      config.installation.operations.length !== NATIVE_PHASE1_OPERATIONS.length ||
      !NATIVE_PHASE1_OPERATIONS.every(operation => config.installation.operations.includes(operation))) {
    throw Error('Native runtime configuration does not contain the locked Phase 1 operation set');
  }
  if (!Array.isArray(config.trust?.issuers) || !config.trust.issuers.length ||
      !config.trust?.keys || typeof config.trust.keys !== 'object' || !Object.keys(config.trust.keys).length) {
    throw Error('Native runtime configuration is missing the pinned gateway trust bundle');
  }
}

async function authenticatedStore(base, root, domain, project, layout, options, localSession) {
  const platform = normalizeStorePlatform(options.platform || 'custom');
  const authPath = sessionPath(root, base, domain);
  let auth = cachedSession(authPath);
  if (auth) {
    try { await activity('Checking saved Auteric session', () => request(base, '/api/commerce/auth/me', { token: auth.access_token }), options); }
    catch (error) { if (!/^(401|403)\b/.test(error.message)) throw error; unlinkSync(authPath); auth = null; }
  }
  recordConnection(root, 'authentication', 'running', { next_action: 'Complete browser pairing before its expiry.' });
  if (!auth) {
    auth = await authenticate(base, options);
    atomicJSON(authPath, { ...auth, expires_at: Date.now() + Math.max(0, Number(auth.expires_in || 0) - 60) * 1000 });
  }
  recordConnection(root, 'authentication', 'complete');
  console.log(terminalColor(`Signed in as ${auth.user.email} (${auth.user.organization})`, 'green', { enabled: activityEnabled(options) }));
  const stores = await activity('Loading Auteric Store status', () => request(base, '/api/commerce/stores', { token: auth.access_token }), options);
  const previous = readConfig(root);
  if (previous?.store_id && (previous.api_url !== base || previous.domain !== domain || !stores.some(item => item.id === previous.store_id && item.domain === domain))) {
    throw Error('Saved Store does not belong to this account or control plane. No new Store was created.');
  }
  let store = stores.find(item => item.domain === domain && (!previous?.store_id || item.id === previous.store_id));
  if (!store) {
    store = await activity('Creating the Auteric Store', () => request(base, '/api/commerce/stores', {
      method: 'POST', token: auth.access_token,
      body: { domain, name: domain, platform, environment: options.environment || (localSession ? 'sandbox' : 'production') },
    }), options);
  }
  if ((store.platform || 'custom') !== platform) throw Error(`Saved Store platform is ${store.platform || 'custom'}, not ${platform}`);
  const state = {
    ...previous, api_url: base, domain, store_id: store.id, framework: project.framework,
    backend_dir: layout.backendRelative, frontend_dir: layout.frontendRelative,
    mode: localSession ? 'local' : 'cloud', local_only: Boolean(localSession && !options.domain),
    status: 'store_registered', platform, adapter: platformAdapter(platform),
  };
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  atomicJSON(configPath(root), state);
  return { auth, store, state };
}

async function completeBrowserPairing(base, auth, storeId) {
  // A cached CLI session has no one-time browser request to complete.  When a
  // pairing page is open, however, this unblocks its handoff to the Connection
  // Test instead of leaving the merchant on a polling screen after Connect.
  if (!auth?.request_id) return;
  await request(base, '/api/commerce/cli/complete', {
    method: 'POST', token: auth.access_token,
    body: { request_id: auth.request_id, store_id: storeId },
  });
}

/** Native Node Phase 1: no bundled Python SDK, no connector config, and no
 * persistent installer process.  The merchant runtime is source-detected,
 * project-tested, registered, health-checked, then (and only then) receives
 * the signed discovery profile. */
async function connectNativeReference(root, options, { base, layout, project, domain, localSession }) {
  if (localSession) throw Error('The native reference flow requires a public HTTPS merchant domain; do not use --localhost.');
  const detected = nativeReference(layout.backend);
  if (!detected.detected) throw Error(`Native reference is incomplete: ${detected.reasons.join('; ')}`);
  recordConnection(root, 'native_reference', 'running', { transport: 'native_http', operations: NATIVE_PHASE1_OPERATIONS });
  if (options['skip-project-checks']) throw Error('Native reference installation requires merchant test and build validation; --skip-project-checks is not allowed.');
  const checks = await runProjectValidation(layout, options);
  const bindingDigest = nativeBindingDigest(layout.backend);
  const { auth, store, state } = await authenticatedStore(base, root, domain, project, layout, options, false);
  const endpoint = `https://${domain}`;
  const operations = nativeOperationEvidence().map(item => ({ ...item, binding_digest: bindingDigest }));
  recordConnection(root, 'native_registration', 'running', { transport: 'native_http', operations: NATIVE_PHASE1_OPERATIONS });
  const installation = await activity('Registering signed Native HTTP runtime', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/installations`, {
      method: 'POST', token: auth.access_token,
      body: {
        environment: store.environment || 'production', transport: 'native_http', endpoint,
        native_runtime: { binding_digest: bindingDigest, trusted_proxy_prefix: null, max_body_bytes: 1048576 },
        protocol_version: '1', sdk_version: packageDependencies(layout.backend)['@auteric/merchant-node'],
        release_id: bindingDigest.slice('sha256:'.length, 'sha256:'.length + 24), operations,
      },
    }), options);
  if (!installation?.id || installation.transport !== 'native_http' || installation.endpoint !== endpoint) {
    throw Error('Control plane did not confirm the expected Native HTTP installation');
  }
  const runtimeConfig = await activity('Fetching pinned native runtime configuration', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/installations/${encodeURIComponent(installation.id)}/native-runtime-config`,
    { token: auth.access_token },
  ), options);
  assertNativeRuntimeConfig(runtimeConfig, { storeId: store.id, environment: store.environment || 'production', endpoint, bindingDigest });
  // This file intentionally contains only public keys and immutable install
  // metadata. Never put operator tokens or Gateway credentials in it.
  atomicJSON(nativeRuntimePath(layout.backend), runtimeConfig, 0o644);
  atomicJSON(nativeInstallationPath(layout.backend), {
    version: 1, transport: 'native_http', installation_id: installation.id, store_id: store.id,
    environment: store.environment || 'production', endpoint, binding_digest: bindingDigest,
    operations: NATIVE_PHASE1_OPERATIONS, project_validation: checks.status,
    runtime_config: '.auteric/native-runtime.json', release_id: installation.release_id || null,
  }, 0o644);
  // Native HTTP has no generated connector mapping to activate. Its immutable
  // registration and local test/build evidence are sufficient to prepare a
  // signed discovery document. Runtime reachability is deliberately checked
  // afterwards: a first Connect must leave the merchant with one complete
  // deployable bundle (runtime config + UCP), not require a second Connect.
  for (const operation of NATIVE_PHASE1_OPERATIONS) {
    await activity(`Preparing native capability: ${operation}`, () => request(
      base, `/api/commerce/stores/${encodeURIComponent(store.id)}/capabilities/${encodeURIComponent(operation)}/enable`,
      { method: 'POST', token: auth.access_token },
    ), options);
  }
  const connectionHealth = await activity('Checking Native HTTP activation readiness', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/connection-health`, { token: auth.access_token },
  ), options);
  if (connectionHealth?.checks?.policies?.state !== 'active') {
    const policy = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/policy`, { token: auth.access_token });
    await activity('Recording reviewed merchant policy', () => request(
      base, `/api/commerce/stores/${encodeURIComponent(store.id)}/policy`, { method: 'PUT', token: auth.access_token, body: policy },
    ), options);
  }
  recordConnection(root, 'native_registration', 'complete', { transport: 'native_http', installation_id: installation.id });
  const discovery = await activity('Preparing signed UCP for one-time deployment', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token },
  ), options);
  const previousDigest = state.discovery_digest || existingDiscoveryDigest(layout.frontend, project.framework, store.id);
  const prepared = prepareDiscovery(layout.frontend, project.framework, discovery.document, { previousDigest });
  const built = prepareBuiltDiscovery(layout.frontend, discovery.document, previousDigest);
  state.discovery_digest = createHash('sha256').update(JSON.stringify(discovery.document, null, 2) + '\n').digest('hex');
  state.mcp_url = discovery.document.auteric_mcp?.endpoint;
  state.integration = 'native_http_verified';
  state.tested_operations = NATIVE_PHASE1_OPERATIONS;
  state.native_installation_id = installation.id;
  state.native_runtime_config = nativeRuntimePath(layout.backend);
  state.status = 'discovery_prepared';
  atomicJSON(configPath(root), state);
  console.log(`Signed UCP prepared at ${prepared.path}.`);
  if (built) console.log(`Built storefront UCP prepared at ${built.path}.`);
  const health = await activity('Verifying deployed Native HTTP runtime', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/installations/${encodeURIComponent(installation.id)}/verify`,
    { method: 'POST', token: auth.access_token },
  ), options);
  if (!health?.reachable) {
    // A clean merchant has not deployed this freshly generated runtime config
    // yet. This is an expected lifecycle state, not a failed integration and
    // never a reason to manufacture public verification or start a connector.
    state.public_discovery_verified = false;
    state.status = 'deployment_pending';
    atomicJSON(configPath(root), state);
    recordConnection(root, 'deployment', 'pending', {
      outcome: 'deployment_pending', transport: 'native_http',
      installation_id: installation.id,
      runtime_config: nativeRuntimePath(layout.backend),
      ucp: prepared.path,
      next_action: 'Deploy this merchant build once, then run Connection Test in Auteric Console.',
    });
    console.log(terminalColor(
      'Native runtime configuration and signed UCP are prepared locally. Deploy this build once, then run Connection Test in Auteric Console. No connector process is required.',
      'green', { enabled: activityEnabled(options) },
    ));
    return state;
  }
  try {
    await activity('Verifying public UCP on the merchant domain', () => request(
      base, `/api/commerce/stores/${encodeURIComponent(store.id)}/verify`, { method: 'POST', token: auth.access_token },
    ), options);
    state.public_discovery_verified = true;
    state.status = 'native_http_ready';
  } catch (error) {
    state.public_discovery_verified = false;
    recordConnection(root, 'discovery', 'publication_pending', {
      outcome: 'incomplete', failure_code: 'public_discovery_not_verified', message: error.message,
      next_action: `Deploy the generated native runtime configuration and UCP, then rerun Connect for ${domain}.`,
    });
  }
  // Publication proves that discovery is live. It does not prove that a real
  // shopper request traversed Gateway, policy, and the merchant runtime. Keep
  // Agent Access off until the operator has reviewed that controlled journey.
  let connectionTestReady = false;
  if (state.public_discovery_verified) {
    const postPublicationHealth = await activity('Checking Connection Test readiness', () => request(
      base, `/api/commerce/stores/${encodeURIComponent(store.id)}/connection-health`, { token: auth.access_token },
    ), options);
    connectionTestReady = postPublicationHealth?.checks?.connection_test?.state === 'active';
    if (connectionTestReady) {
      await activity('Activating Gateway access for verified Native HTTP runtime', () => request(
        base, `/api/commerce/stores/${encodeURIComponent(store.id)}/agent-access`, { method: 'PUT', token: auth.access_token, body: { enabled: true } },
      ), options);
    }
  }
  atomicJSON(configPath(root), state);
  const onboardingReady = state.public_discovery_verified && connectionTestReady;
  state.status = onboardingReady ? 'native_http_ready' : state.public_discovery_verified ? 'connection_test_required' : 'discovery_prepared';
  recordConnection(root, 'discovery', state.public_discovery_verified ? 'public_verified' : 'publication_pending', {
    outcome: onboardingReady ? 'native_ready' : state.public_discovery_verified ? 'connection_test_required' : 'discovery_pending', transport: 'native_http',
    // A rerun may follow a publication-pending attempt.  Clear its diagnostic
    // fields once the public verification succeeds so terminal exit status and
    // connect-status describe the current run rather than stale failure state.
    failure_code: state.public_discovery_verified ? null : 'public_discovery_not_verified',
    message: state.public_discovery_verified ? 'Native HTTP runtime and public UCP verification passed.' : null,
    next_action: onboardingReady
      ? 'Native HTTP installation is active; no connector process is required.'
      : state.public_discovery_verified
        ? `Run the controlled Connection Test at ${base}/console?store=${encodeURIComponent(store.id)}&onboarding=connection-test, then rerun Connect to activate Agent Access.`
        : `Deploy the prepared UCP at https://${domain}/.well-known/ucp and retry.`,
  });
  try {
    await completeBrowserPairing(base, auth, store.id);
  } catch (error) {
    // The connection evidence remains authoritative even if a browser tab was
    // closed or a legacy control plane cannot receive the handoff callback.
    console.log(`Dashboard handoff is unavailable; open ${base}/console?store=${encodeURIComponent(store.id)}&onboarding=connection-test`);
  }
  const completion = onboardingReady
    ? 'Native HTTP runtime, public UCP, and Connection Test verification passed. Agent Access is active; no connector process is required.'
    : state.public_discovery_verified
      ? `Native HTTP runtime and public UCP verification passed. Run the controlled Connection Test: ${base}/console?store=${encodeURIComponent(store.id)}&onboarding=connection-test`
    : 'Native runtime passed its health check; public UCP publication is pending deployment. No connector was started.';
  console.log(terminalColor(completion, onboardingReady ? 'green' : state.public_discovery_verified ? 'green' : 'red', { enabled: activityEnabled(options) }));
  return state;
}

async function connectServiceFirst(root, options, context) {
  const { base, layout, project, domain, localSession, storeUrl, backendUrl, inventory } = context;
  if (options['skip-project-checks']) throw Error('Service-First installation requires merchant test and build validation; --skip-project-checks is not allowed.');
  const checks = await runProjectValidation(layout, options);
  recordConnection(root, 'service_first_validation', 'running', { operations: CUSTOM_MVP_OPERATIONS });
  let verification = [];
  if (localSession) {
    if (!storeUrl) throw Error('Service-First sandbox verification requires --store-url or --local-storefront');
    verification = await activity('Testing catalog, cart and pre-payment checkout through the merchant API',
      () => verifyStandardMerchantBridge(storeUrl), options);
  }
  const { auth, store, state } = await authenticatedStore(base, root, domain, project, layout, options, localSession);
  const bindingDigest = serviceFirstBindingDigest();
  // The registered endpoint is the merchant origin that will reverse-proxy
  // /api/auteric/v1 after deployment. Even a local sandbox registration uses
  // its non-public test hostname; loopback execution is verification evidence,
  // not the deployment identity.
  const endpoint = `https://${domain}`;
  const operations = serviceFirstOperationEvidence(bindingDigest);
  const installation = await activity('Registering the Service-First sidecar installation', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/installations`, {
      method: 'POST', token: auth.access_token,
      body: {
        environment: store.environment || (localSession ? 'sandbox' : 'production'),
        transport: 'native_http', endpoint,
        native_runtime: { binding_digest: bindingDigest, trusted_proxy_prefix: null, max_body_bytes: 1048576 },
        protocol_version: '1', sdk_version: 'merchant-sidecar/0.1.0',
        release_id: bindingDigest.slice(7, 31), operations,
      },
    }), options);
  const runtimeConfig = await activity('Fetching pinned sidecar trust configuration', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/installations/${encodeURIComponent(installation.id)}/native-runtime-config`,
    { token: auth.access_token },
  ), options);
  if (runtimeConfig?.installation?.bindingDigest !== bindingDigest || runtimeConfig?.installation?.storeId !== store.id) {
    throw Error('Control plane returned a sidecar runtime configuration for a different binding or Store');
  }
  const bundle = prepareServiceFirstBundle(layout.backend, {
    runtimeConfig, merchantBaseUrl: backendUrl, operations: CUSTOM_MVP_OPERATIONS, verification,
    releaseId: installation.release_id,
  });
  for (const operation of CUSTOM_MVP_OPERATIONS) {
    await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/capabilities/${encodeURIComponent(operation)}/enable`, {
      method: 'POST', token: auth.access_token,
    });
  }
  const health = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/connection-health`, { token: auth.access_token });
  if (health?.checks?.policies?.state !== 'active') {
    const policy = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/policy`, { token: auth.access_token });
    await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/policy`, {
      method: 'PUT', token: auth.access_token, body: policy,
    });
  }
  const discovery = await activity('Preparing signed UCP for the deployable sidecar', () => request(
    base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token },
  ), options);
  const previousDigest = state.discovery_digest || existingDiscoveryDigest(layout.frontend, project.framework, store.id);
  const prepared = prepareDiscovery(layout.frontend, project.framework, discovery.document, { previousDigest });
  const built = prepareBuiltDiscovery(layout.frontend, discovery.document, previousDigest);
  Object.assign(state, {
    discovery_digest: createHash('sha256').update(JSON.stringify(discovery.document, null, 2) + '\n').digest('hex'),
    mcp_url: discovery.document.auteric_mcp?.endpoint,
    integration: 'service_first_sidecar', status: 'deployment_pending',
    tested_operations: verification.map(item => item.operation),
    sidecar_installation_id: installation.id, sidecar_bundle: bundle.directory,
    project_validation: checks.status, adapter_profile: serviceFirstSupport(inventory).profile,
    public_discovery_verified: false,
  });
  atomicJSON(configPath(root), state);
  recordConnection(root, 'deployment', 'pending', {
    outcome: 'deployment_pending', transport: 'native_http', installation_id: installation.id,
    bundle: bundle.directory, ucp: prepared.path,
    next_action: 'Deploy the generated bridge and sidecar bundle with this merchant release, then run Connection Test.',
  });
  try { await completeBrowserPairing(base, auth, store.id); } catch (error) {
    console.log(`Dashboard handoff is unavailable; open ${base}/console?store=${encodeURIComponent(store.id)}&onboarding=connection-test`);
  }
  console.log(`Service-First bundle prepared at ${bundle.directory}.`);
  console.log(`Signed UCP prepared at ${prepared.path}.${built ? ` Built copy: ${built.path}.` : ''}`);
  console.log(`Verified without payment: ${state.tested_operations.join(', ') || 'none (production verification remains required)'}.`);
  console.log('Deploy once, expose only the sidecar at /api/auteric/v1, then run Connection Test. Payment and complete_checkout remain disabled.');
  return state;
}

async function connect(root, options) {
  const base = apiUrl(options);
  const platform = normalizeStorePlatform(options.platform || 'custom');
  const localSession = validateConnectOptions(options);
  const storeUrl = options['store-url']
    ? localStoreUrl(options['store-url'])
    : options['local-storefront'] ? await detectLocalStoreUrl() : null;
  if (!options['dry-run']) connectionStatus(root, { control_plane: base, local_storefront: storeUrl, mode: localSession ? 'local_sandbox' : 'cloud' });
  const layout = resolveProjectLayout(root, options);
  const project = inspect(layout.frontend);
  const localOnly = Boolean(localSession && !options.domain);
  const domain = localOnly ? localTestDomain(root) : domainName(options.domain);
  const backendUrl = options['backend-url']
    ? (!options['legacy-connector'] && options.deployment
      ? (await shared('mapping')).backendOrigin(options['backend-url'], { privateHosts: [readJSON(resolve(root, options.deployment))?.merchant_service].filter(Boolean) })
      : (options.localhost ? localStoreUrl(options['backend-url']) : apiUrl({ 'api-url': options['backend-url'] })))
    : storeUrl || (localOnly ? null : `https://${domain}`);
  const probeUrl = localSession ? backendUrl : null;
  if (project.existingUcp.length && readConfig(root)?.domain !== domain)
    throw Error(`UCP already exists: ${project.existingUcp.join(', ')}. Review before connecting.`);
  const agent = options['no-agent'] ? 'none' : options.agent || 'auto';
  if (!['codex', 'claude', 'cursor', 'copilot', 'none', 'auto'].includes(agent)) throw Error('Use --agent codex|claude|cursor|auto|none');
  console.log(`Store: ${domain} | Platform: ${platform} | Frontend: ${layout.frontendRelative} | Backend: ${layout.backendRelative} | Framework: ${project.framework} | Instructions: ${agent === 'auto' ? 'Codex/Copilot, Claude, Cursor' : agent}`);
  if (localOnly) console.log('This is a local test identifier, not a public domain or ownership proof.');
  if (options['local-storefront']) console.log(`Local storefront detected at ${storeUrl}; using the remote Auteric control plane in sandbox mode.`);
  console.log(options['legacy-connector'] ? 'Connect inventories APIs and prepares legacy connector adapters.' : 'Connect maps supported HTTP operations to a shared runtime; unsupported contracts return a precise gap.');
  console.log('Candidate commerce libraries:', project.catalogCandidate.join(', ') || 'none detected');
  if (options['dry-run']) {
    console.log('Dry run: no authentication, store creation or file changes.');
    if (platform === 'custom' && options.mapping) return prepareHTTP(root, options, { base, layout, domain, request, dryRun: true });
    return;
  }
  if (platform !== 'custom') {
    if (localSession) throw Error('Official platform connectors require cloud onboarding and a real store domain.');
    const { auth, store, state } = await authenticatedStore(base, root, domain, project, layout, options, false);
    state.integration = platform === 'shopify' ? 'platform_install_required' : 'connector_not_shipped';
    state.status = 'platform_connection_pending';
    atomicJSON(configPath(root), state);
    try { await completeBrowserPairing(base, auth, store.id); }
    catch (error) { if (!/^404\b/.test(error.message)) throw error; }
    console.log(platform === 'shopify'
      ? `Shopify Store registered. Continue the official app installation at ${base}/console?store=${encodeURIComponent(store.id)}&onboarding=connection.`
      : `${platformAdapter(platform).adapter_family} is selected, but this connector is not shipped in this release. No custom adapter or production capability was activated.`);
    return state;
  }
  // Native reference detection has priority over legacy connector state.  This
  // branch must stay before `sdk('prepare', ...)`: the bundled Python SDK is
  // the outbound connector installer and is not part of native commerce.
  if (nativeReference(layout.backend).detected) {
    console.log(`Native reference detected: Node/Express Native HTTP (${NATIVE_PHASE1_OPERATIONS.join(', ')}).`);
    return connectNativeReference(root, options, { base, layout, project, domain, localSession });
  }
  if (!options['legacy-connector']) {
    if (options.environment && !['dev', 'sandbox', 'staging', 'production'].includes(options.environment)) throw Error('Unsupported environment');
    if (options.serve) throw Error('HTTP Connect prepares a managed deployment; --serve is only supported with --legacy-connector');
    try {
      return await prepareHTTP(root, options, { base, layout, domain, request,
        authenticate: () => authenticatedStore(base, root, domain, project, layout, options, localSession) });
    } catch (error) {
      const message = failureDetails(error).message;
      const status = /^(implementation_required|selection_required|release_unqualified|runtime_api_incompatible|deployment_prerequisite_missing|environment_required|storage_profile_unsupported|credential_delivery_required|artifact_conflict|inventory_incomplete|bridge_test_required|bridge_test_failed):/.exec(message)?.[1] || 'implementation_required';
      console.log(message.startsWith(status + ':') ? message : `${status}: ${message}`);
      return prepareResult(root, status, { message, next_action: 'Resolve the precise mapping or release prerequisite and rerun Connect. No capability is activated.' });
    }
  }
  const serviceInventory = await inventoryRepo(layout.backend, { backendDir: options.backend });
  const serviceFirst = serviceFirstSupport(serviceInventory);
  if (serviceFirst.supported) {
    console.log(`Service-First bridge profile detected (${CUSTOM_MVP_OPERATIONS.length} catalog/cart/checkout operations; payment excluded).`);
    return connectServiceFirst(root, options, {
      base, layout, project, domain, localSession, storeUrl, backendUrl, inventory: serviceInventory,
    });
  }
  recordConnection(root, 'inspection', 'running');
  let prepared = await activity('Inspecting APIs and preparing adapter evidence', () => sdk('prepare', layout.backend, { agent: agent === 'claude' ? 'claude-code' : agent === 'copilot' ? 'codex' : agent,
    store_url: probeUrl, instructions_root: root }, { install: true }), options);
  // Older local SDKs returned one skill result while the bundled runtime
  // returns a list.  Accept both so a current CLI can safely drive either.
  const preparedSkills = Array.isArray(prepared.skill) ? prepared.skill : (prepared.skill ? [prepared.skill] : []);
  const conflicts = preparedSkills.filter(item => item.conflict).map(item => item.client);
  if (conflicts.length) console.log(`Existing assistant instructions preserved for: ${conflicts.join(', ')}. Review these files manually.`);
  const summary = prepared.inventory.inventory_summary || {};
  console.log(`Inspected ${prepared.inventory.files_inspected} backend files and inventoried ${summary.api_endpoints ?? summary.total_endpoints ?? 0} APIs plus ${summary.storefront_routes ?? 0} storefront routes; ${summary.tool_candidates ?? 0} are canonical shopping-tool candidates. Capability report: ${join(layout.backend, '.auteric/capabilities.json')}`);
  recordConnection(root, 'inspection', 'complete');
  let assistantAttempted = false;
  if (!prepared.connector_prepared && agent !== 'none') {
    const assistant = await prepareWithAgent(root, layout.backend, backendUrl, agent);
    assistantAttempted = true;
    console.log(`Adapter preparation: ${assistant.status}`);
    prepared = await activity('Rechecking adapter evidence', () => sdk('prepare', layout.backend, { agent: 'none', store_url: probeUrl, instructions_root: root }), options);
  }
  if (!prepared.connector_prepared) {
    recordConnection(root, 'adapter', 'implementation_required', { outcome: 'incomplete', next_action: 'Implement the reported connector mappings, then retry.' });
    for (const reason of prepared.inventory.connector_diagnostics || []) console.log(`Diagnosis: ${reason}`);
    console.log('Integration incomplete: no commerce connector is configured. The coding agent must trace the detected APIs, implement .auteric/connector.json and test its handlers before rerunning Connect. No new Store or UCP was created.');
    return { integration: 'implementation_required', tested_operations: [] };
  }
  if (options['skip-project-checks']) {
    console.log('Project test suite and build checks were skipped by --skip-project-checks.');
  } else {
    const projectChecks = await runProjectValidation(layout, options);
    console.log(`Project validation passed: ${projectChecks.checks.map(check => check.command).join(', ') || 'no package scripts found'}.`);
  }
  recordConnection(root, 'local_validation', 'running');
  let local = await activity('Validating local adapter contracts', () => sdk('local-check', layout.backend, { development: localSession }), options);
  let pending = uncoveredOperations(prepared.inventory, local.prepared_operations);
  if (local.status === 'local_contract_passed' && pending.length && agent !== 'none' && !assistantAttempted) {
    const assistant = await prepareWithAgent(root, layout.backend, backendUrl, agent, { missingOperations: pending });
    assistantAttempted = true;
    console.log(`Capability completion: ${assistant.status}`);
    prepared = await activity('Rechecking completed adapter evidence', () => sdk('prepare', layout.backend, { agent: 'none', store_url: probeUrl, instructions_root: root }), options);
    local = await activity('Revalidating local adapter contracts', () => sdk('local-check', layout.backend, { development: localSession }), options);
    pending = uncoveredOperations(prepared.inventory, local.prepared_operations);
  }
  recordConnection(root, 'local_validation', local.status);
  if (pending.length) console.log(`Unconnected source candidates (not exposed as tools): ${pending.join(', ')}. Their business/session contracts still require an adapter.`);
  if (local.status !== 'local_contract_passed') {
    console.log(`Adapter validation: ${local.status}. See .auteric/local-validation.json; authentication has not started.`);
    return { integration: local.status, tested_operations: local.tested_operations || [] };
  }
  const authPath = sessionPath(root, base, domain);
  let auth = cachedSession(authPath);
  if (auth) {
    try { await activity('Checking saved Auteric session', () => request(base, '/api/commerce/auth/me', { token: auth.access_token }), options); }
    catch (error) { if (!/^(401|403)\b/.test(error.message)) throw error; unlinkSync(authPath); auth = null; }
  }
  recordConnection(root, 'authentication', 'running', { next_action: 'Complete browser pairing before its expiry.' });
  if (!auth) {
    auth = await authenticate(base, options);
    atomicJSON(authPath, { ...auth, expires_at: Date.now() + Math.max(0, Number(auth.expires_in || 0) - 60) * 1000 });
  }
  recordConnection(root, 'authentication', 'complete');
  console.log(terminalColor(`Signed in as ${auth.user.email} (${auth.user.organization})`, 'green', { enabled: activityEnabled(options) }));
  const stores = await activity('Loading Auteric Store status', () => request(base, '/api/commerce/stores', { token: auth.access_token }), options);
  const previous = readConfig(root);
  if (previous?.store_id && (previous.api_url !== base || previous.domain !== domain || !stores.some(item => item.id === previous.store_id && item.domain === domain)))
    throw Error('Saved Store does not belong to this account or control plane. No new Store was created.');
  let store = stores.find(item => item.domain === domain && (!previous?.store_id || item.id === previous.store_id));
  if (!store) {
    store = await activity('Creating the Auteric Store', () => request(base, '/api/commerce/stores', { method: 'POST', token: auth.access_token,
      body: { domain, name: domain, platform, environment: localSession ? 'sandbox' : 'production' } }), options);
  }
  if ((store.platform || 'custom') !== platform) throw Error(`Saved Store platform is ${store.platform || 'custom'}, not ${platform}`);
  const state = { ...previous, discovery_digest: readConfig(root)?.discovery_digest, api_url: base, domain, store_id: store.id, platform, adapter: platformAdapter(platform), framework: project.framework, backend_dir: layout.backendRelative, frontend_dir: layout.frontendRelative, mode: localSession ? 'local' : 'cloud', local_only: localOnly, status: 'store_registered' };
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  atomicJSON(configPath(root), state);
  // Production traffic is intentionally blocked until the merchant domain
  // serves the exact signed discovery document. Prepare that document before
  // running the end-to-end connection test so first-time stores do not enter a
  // discovery/test deadlock.
  if (!localSession && options['approve-publication']) {
    const bootstrap = await activity('Getting the signed UCP profile', () => request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token }), options);
    if (bootstrap?.document) {
      const previousDigest = state.discovery_digest || existingDiscoveryDigest(layout.frontend, project.framework, store.id);
      const result = prepareDiscovery(layout.frontend, project.framework, bootstrap.document, { previousDigest });
      const built = prepareBuiltDiscovery(layout.frontend, bootstrap.document, previousDigest);
      state.discovery_digest = createHash('sha256').update(JSON.stringify(bootstrap.document, null, 2) + '\n').digest('hex');
      state.mcp_url = bootstrap.document.auteric_mcp?.endpoint;
      atomicJSON(configPath(root), state);
      console.log(`Signed UCP prepared at ${result.path}.`);
      if (built) console.log(`Built storefront UCP prepared at ${built.path}.`);
      try {
        await activity('Verifying public UCP on the merchant domain', () => request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/verify`, { method: 'POST', token: auth.access_token }), options);
        state.public_discovery_verified = true;
        atomicJSON(configPath(root), state);
        console.log(`Public discovery verified at https://${domain}/.well-known/ucp.`);
      } catch (error) {
        state.integration = 'publication_pending';
        state.status = 'discovery_prepared';
        atomicJSON(configPath(root), state);
        recordConnection(root, 'discovery', 'publication_pending', {
          outcome: 'incomplete',
          next_action: `Publish the prepared UCP at https://${domain}/.well-known/ucp, then rerun Connect.`,
          failure_code: 'public_discovery_not_verified',
          message: error.message,
        });
        console.log(`Publication pending: publish the exact generated JSON at https://${domain}/.well-known/ucp, then rerun Connect.`);
        return state;
      }
    }
  }
  recordConnection(root, 'runtime_validation', 'running');
  const digest = projectDigest(layout.backend);
  const savedValidation = readJSON(join(layout.backend, '.auteric/validation.json'));
  let validation;
  if (previous?.integration === 'locally_tested' && previous.project_digest === digest && previous.credential_file && existsSync(previous.credential_file) && savedValidation?.status === 'locally_tested') {
    const active = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/mappings`, { token: auth.access_token });
    if (!savedValidation.mapping_versions?.length || !savedValidation.mapping_versions.every(item => active.some(row => row.id === item.id && row.state === 'active')))
      throw Error('Previously tested mappings were changed or disabled. Review their status before activating access again.');
    validation = savedValidation;
    console.log('Resuming previously tested mappings; no merchant mutation tests are replayed.');
  } else {
    if (previous?.integration && local.prepared_operations?.some(op => !['search_products', 'get_product', 'get_cart', 'get_checkout'].includes(op)))
      throw Error('This changed or interrupted connector includes merchant writes. Reconcile the previous test run before repeating those operations.');
    validation = await activity('Running mapping, gateway and MCP tests', () => sdk('connect', layout.backend, { api_url: base, store_id: store.id, token: auth.access_token,
      environment: store.environment || (localSession ? 'sandbox' : 'production'), development: localSession,
      publication_approved: Boolean(options['approve-publication']) }), options);
  }
  state.project_digest = digest;
  state.unconnected_candidates = pending;
  recordConnection(root, 'runtime_validation', validation.status, { tested_operations: validation.tested_operations || [] });
  state.integration = validation.status;
  state.tested_operations = validation.tested_operations;
  state.credential_file = validation.credential_file;
  console.log(`Integration: ${validation.status}; tested operations: ${validation.tested_operations.join(', ') || 'none'}`);
  if (validation.status !== 'locally_tested') {
    atomicJSON(configPath(root), state);
    connectionStatus(root, { outcome: 'incomplete', next_action: 'Inspect .auteric/validation.json and finish the reported adapter or publication prerequisite.' });
    console.log('Setup is incomplete. No new UCP was generated. Inspect the validation report and finish the required adapter tests or production preparation.');
    return state;
  }
  atomicJSON(configPath(root), state);
  recordConnection(root, 'discovery', 'running');
  let discovery;
  try { discovery = await activity('Refreshing the signed UCP profile', () => request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token }), options); }
  catch (error) { console.log(`UCP publication pending: ${error.message}`); }
  if (discovery?.document) {
    const previousDigest = readConfig(root)?.discovery_digest || existingDiscoveryDigest(layout.frontend, project.framework, store.id);
    const result = prepareDiscovery(layout.frontend, project.framework, discovery.document, {
      previousDigest,
    });
    const built = prepareBuiltDiscovery(layout.frontend, discovery.document, previousDigest);
    state.discovery_digest = createHash('sha256').update(JSON.stringify(discovery.document, null, 2) + '\n').digest('hex');
    console.log(`Signed UCP prepared at ${result.path}.`);
    if (built) console.log(`Built storefront UCP prepared at ${built.path}.`);
    state.mcp_url = discovery.document.auteric_mcp?.endpoint;
    state.status = validation.status === 'locally_tested' ? 'capabilities_prepared' : validation.status;
    // Persist the tested connector and signed profile before probing a dev server.
    // A transient route failure must not erase successfully completed setup.
    atomicJSON(configPath(root), state);
    if (storeUrl) {
      const check = await verifyLocalWithRetry(base, store.id, auth.access_token, storeUrl, discovery.document);
      if (check.local_verified && !check.public_domain_verified) {
        state.local_discovery_verified = true;
        console.log(`Local UCP verified at ${check.url}. Public ownership and runtime protection remain unverified.`);
      } else {
        state.local_discovery_verified = false;
        state.status = 'local_discovery_pending';
        console.log(`Local discovery pending: ${check.reason}`);
      }
    }
    if (!localSession || state.local_discovery_verified) {
      const gateway = await provisionGatewayAccess(root, base, domain, store.id, auth.access_token,
        validation.tested_operations, state.mcp_url);
      if (gateway) {
        state.gateway_credential_file = gateway.credential_file;
        state.gateway_credential_id = gateway.credential_id;
        state.gateway_operations = gateway.operations;
        console.log(`Gateway MCP read-only grant ready for ${gateway.operations.join(', ')}. Private credential: ${gateway.credential_file}`);
      }
    }
  }
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  atomicJSON(configPath(root), state);
  const discoveryVerified = localSession ? state.local_discovery_verified : state.public_discovery_verified;
  recordConnection(root, 'discovery', discoveryVerified ? (localSession ? 'local_verified' : 'public_verified') : 'pending', {
    outcome: discoveryVerified ? (localSession ? 'local_ready' : 'connector_required') : 'discovery_pending',
    next_action: discoveryVerified ? 'Start and keep the persistent connector running.' : `Publish the exact signed UCP JSON at https://${domain}/.well-known/ucp, then retry.`,
    failure_code: discoveryVerified ? null : 'public_discovery_not_verified',
    message: discoveryVerified ? null : 'The current signed UCP has not yet been verified at the merchant domain.',
  });
  try {
    await request(base, '/api/commerce/cli/complete', { method: 'POST', token: auth.access_token,
      body: { request_id: auth.request_id, store_id: store.id } });
  } catch (error) {
    if (!/^404\b/.test(error.message)) throw error;
    console.log('This control plane does not support automatic dashboard handoff yet. Open the dashboard link below.');
  }
  console.log(`Store created or resumed: ${store.id}. Local configuration: ${configPath(root)}`);
  console.log(state.public_discovery_verified
    ? 'Public UCP publication and production verification passed; the persistent connector must remain running.'
    : 'Public publication and production verification are pending.');
  if (validation.credential_file) console.log(`Start the persistent local connector using the same CLI entrypoint: node ${process.argv[1]} connector`);
  console.log(`Your store dashboard: ${base}/console?store=${encodeURIComponent(store.id)}`);
  if (localSession) console.log('Local test mode: localhost URLs and sandbox validation are not production trust or HTTPS merchant discovery.');
  if (options.serve && state.local_discovery_verified && state.credential_file) {
    console.log('Connector running outbound. Keep this terminal open; Ctrl-C stops it.');
    await sdk('serve', layout.backend, { credential_file: state.credential_file });
  } else if (options.serve) {
    console.log('Connector was not started: local discovery is unverified. Resolve the reported route issue, then rerun Connect.');
  }
  return state;
}

export async function run(argv, root = process.cwd()) {
  root = resolve(root);
  const { command, options } = parse(argv);
  const progress = cliProgress(options);
  if(command==='connect')validateConnectOptions(options);
  const moduleConfig = readJSON(join(root,'auteric/connection.json'))?.schema === 'auteric-module-connection/v1';
  const httpConfig = readJSON(join(root,'auteric/connection.json'))?.schema === 'auteric-connection/v1'
    || existsSync(join(root,'auteric/mapping.json'));
  if(command === 'disconnect' && (moduleConfig || readJSON(join(root,'auteric/.state/config.json'))?.integration === 'module' || existsSync(join(root,'auteric/.state/hooks.json')))) {
    const state=readJSON(join(root,'auteric/.state/config.json'));
    const result=await disconnectModule(root,{request,
      authenticate:async()=>{
        const path=sessionPath(root,state.api_url,state.domain);
        let auth=cachedSession(path);
        if(auth?.access_token) {
          try {await request(state.api_url,'/api/commerce/auth/me',{token:auth.access_token});return auth;}
          catch(error){if(!/^(401|403)\b/.test(String(error.message)))throw error;}
        }
        auth=await authenticate(state.api_url,options);
        atomicJSON(path,{...auth,expires_at:Date.now()+Math.max(0,Number(auth.expires_in||0)-60)*1000});return auth;
      },
      stopCompose:path=>runProcess('docker',['compose','-f',path,'stop','--timeout','30','auteric-runtime'],root),
    });console.log(JSON.stringify(result,null,2));return result;
  }
  if(command === 'connect' && !options['dry-run'] && !options['legacy-connector'] && !options.mapping && !httpConfig &&
    (options['local-acceptance'] || options['adapter-plan'] || moduleConfig || ((!options.platform || normalizeStorePlatform(options.platform)==='custom') && !nativeReference(root).detected))) {
    initializeManaged(root);
    const release=lockProject(root,MANAGED_STATE);
    try {
    const pendingPlan=join(root,'auteric/.state/adapter-plan.json');
    const planPath=options['adapter-plan'] ? resolve(root,options['adapter-plan']) : (!moduleConfig && existsSync(pendingPlan) ? pendingPlan : null);
    if(planPath) await installModule(root,readJSON(planPath));
    if(!existsSync(join(root,'auteric/connection.json'))) {
      const dossier=await integrationDossier(root);
      const result={integration:'implementation_required',status:'implementation_required',production_ready:false,tested_operations:[],registry_digest:dossier.registry_digest,capabilities:dossier.capabilities.length,
        next_action:dossier.instructions,dossier:'auteric/.state/model-dossier.json',
        continuation:{owner:'current_coding_model',skill:[new URL('../plugins/auteric-kit/skills/auteric-connect/SKILL.md',import.meta.url),new URL('../../../SKILL.md',import.meta.url)].find(path=>existsSync(path))?.pathname,
          plan:'auteric/.state/adapter-plan.json',resume_command:['connect',...Object.entries(options).flatMap(([key,value])=>value===true ? ['--'+key] : ['--'+key,String(value)])],
          stop_for_user:false}};
      console.log(JSON.stringify(result,null,2));return result;
    }
    const result=await localAcceptance(root,options);
    const deployment=options.deployment || (existsSync(join(root,'auteric/deployment.json')) ? 'auteric/deployment.json' : null);
    if(deployment) {
      const base=apiUrl(options),domain=options.domain;
      if(!domain)throw Error('domain_required: model must preserve the requested merchant domain');
      const binding=await (await shared('module-adapter')).moduleBinding(join(root,'auteric/connection.json'));
      const layout={root,backend:root,frontend:root};
      const prepared=await prepareHTTP(root,{...options,deployment},{base,domain,layout,request,moduleBinding:binding,localReport:result,
        authenticate:()=>authenticatedStore(base,root,domain,inspect(root),layout,options,false)});
      console.log(JSON.stringify(prepared,null,2));return prepared;
    }
    console.log(JSON.stringify({status:result.status,operations:result.operations,scenarios:result.scenarios.length,
      runtime_image_id:result.runtime_image_id,local_only:true,production_ready:false,report:'auteric/.state/acceptance.json'},null,2));return result;
    } finally {release();}
  }
  if (command === 'inventory') {
    const target = resolve(root, options._path || '.');
    if (!isDirectory(target)) throw Error(`Inventory target is not a directory: ${options._path || '.'}`);
    progress.phase('Inventory', 'started', { detail: 'Inventory: scanning backend sources' });
    const report = await inventoryRepo(target, { backendDir: options.backend });
    progress.phase('Inventory', 'complete', {
      detail: `Inventory: ${report.summary.endpoints_found} endpoints; ${report.summary.commerce_candidates} commerce candidates; ${report.summary.need_review ?? (report.need_review || []).length} need review.`,
      files_count: report.budget?.files_read,
    });
    console.log(JSON.stringify(report, null, 2));
    return report;
  }
  if (command === 'bind') {
    const target = resolve(root, options._path || '.');
    if (!isDirectory(target)) throw Error(`Bind target is not a directory: ${options._path || '.'}`);
    progress.phase('Binding', 'started', { detail: 'Binding: inventory, plan, generate, reconcile, validate' });
    const result = await bindRepo(target, {
      backendDir: options.backend,
      platform: options.platform || 'custom',
      requireMerchantSelection: true,
      approveCandidates: Boolean(options['approve-adapters']),
      approvedBy: options['approved-by'] || 'merchant-cli',
    });
    const registry = loadOperationsRegistry(result.plan.registry?.path);
    for (const binding of result.plan.bindings) {
      const symbol = binding.symbol || binding.proposed_symbol;
      const ownership = registry?.operations?.[binding.operation]?.ownership?.rule ? '; ownership path found.' : '';
      progress.phase('Binding', 'advanced', { operation: binding.operation, detail: `Binding: ${binding.operation} → ${symbol ? `${symbol.name}.${symbol.method}` : binding.action}${ownership}` });
    }
    progress.phase('Generated', 'advanced', {
      detail: `Generated: ${fraction(result.summary.bound, result.summary.found)} adapters${result.summary.merchant_decisions ? `; ${result.summary.merchant_decisions} awaiting merchant decision.` : '.'}`,
      files_count: result.summary.files_written,
    });
    progress.asVerified().phase('Validation', result.validation.ok ? 'complete' : 'failed', {
      detail: `Validation: ${fraction(result.validation.operations.verified, result.validation.operations.bound)} operations verified${result.validation.errors.length ? `; ${result.validation.errors.length} problem(s).` : '.'}`,
      evidence_ref: '.auteric/installation.json',
    });
    for (const line of bindingSummaryLines(result)) console.log(line);
    return { binding: result.summary.pending ? 'pending' : 'verified', ...result.summary };
  }
  if (command === 'validate') {
    const target = resolve(root, options._path || '.');
    if (!isDirectory(target)) throw Error(`Validate target is not a directory: ${options._path || '.'}`);
    const result = validateInstallation(target);
    for (const error of result.errors) console.error(`${error.check}: ${error.message}`);
    console.log(result.ok
      ? `Validation passed: ${result.operations.verified}/${result.operations.bound} operations verified`
      : `Validation failed: ${result.errors.length} problem(s), ${result.operations.verified}/${result.operations.bound} operations verified`);
    if (!result.ok) throw Error('Installation validation failed');
    return { validation: 'passed', ...result.operations };
  }
  if (command === 'inspect') {
    const layout = resolveProjectLayout(root, options);
    console.log(JSON.stringify({ layout, inventory: await sdk('inspect', layout.backend) }, null, 2));
    return;
  }
  if (command === 'connector') {
    const state = readConfig(root);
    if (!state?.credential_file) throw Error('Run connect and complete local contract tests first.');
    console.log('Connector running outbound. Keep this process running; Ctrl-C stops it.');
    const backend = state.backend_dir ? resolve(root, state.backend_dir) : root;
    return sdk('serve', backend, { credential_file: state.credential_file }, { install: true });
  }
  if (command === 'connect') {
    if (options['dry-run']) return connect(root, options);
    validateConnectOptions(options);
    const layout=resolveProjectLayout(root,options);
    const managed=normalizeStorePlatform(options.platform||'custom')==='custom' && !options['legacy-connector'] && !nativeReference(layout.backend).detected;
    const directory=managed?MANAGED_STATE:'.auteric';
    if(managed)initializeManaged(root);
    const release = lockProject(root,directory);
    try {
      connectionStatus(root, { version: 1, run_id: randomBytes(12).toString('hex'), status: 'running', phase: 'starting', outcome: 'pending', started_at: new Date().toISOString(), next_action: 'Connect is running. Follow the current phase and complete pairing when requested.' },directory);
      return await connect(root, options);
    }
    catch (error) {
      const failure = failureDetails(error);
      recordConnection(root, 'connection', 'failed', { outcome: 'failed', ...failure },directory);
      throw error;
    }
    finally { release(); }
  }
  if (command === 'login') { const auth = await authenticate(apiUrl(options), options); console.log(terminalColor(`Signed in as ${auth.user.email}. This session is held only for this command; use connect to register a store.`, 'green', { enabled: activityEnabled(options) })); return; }
  if (command === 'connect-status') { console.log(JSON.stringify(readJSON(join(root,stateDirectory(root),'connection-status.json')) || { status: 'not_started' }, null, 2)); return; }
  if (command === 'logout') { const state = readConfig(root); if (state) { const path = sessionPath(root, state.api_url, state.domain); if (existsSync(path)) unlinkSync(path); } console.log('Project CLI session removed. Browser sessions and connector access are managed in the Auteric console.'); return; }
  if (command === 'verify' && (options._path !== undefined || (!readConfig(root) && existsSync(join(root, '.auteric', 'installation.json'))))) {
    // Contract acceptance on a bound installation: static validation, then
    // dev-mode execution of the locked scenarios over real HTTP.
    const target = resolve(root, options._path || '.');
    if (!isDirectory(target)) throw Error(`Verify target is not a directory: ${options._path || '.'}`);
    const port = options.port === undefined ? 0 : Number(options.port);
    if (!Number.isInteger(port) || port < 0 || port > 65535) throw Error('--port needs an integer between 0 and 65535');
    const result = await runAcceptance(target, { port, progress });
    for (const line of acceptanceSummaryLines(result)) console.log(line);
    return { acceptance: result.summary.failed || result.summary.pending ? 'incomplete' : 'verified', ...result.summary };
  }
  if (command === 'status' || command === 'doctor' || command === 'verify') {
    const state = readConfig(root);
    const project = inspect(root);
    if (command === 'doctor') {
      console.log(JSON.stringify({ integration: state?.integration || 'not_connected', tested_operations: state?.tested_operations || [], node: process.version, framework: project.framework, agents: project.agents, existingUcp: project.existingUcp, api_url: apiUrl(options) }, null, 2));
      return;
    }
    if (!state) throw Error('No Auteric connection. Run auteric connect --localhost for a local store, or supply --domain for a public store.');
    if (command === 'status') { console.log(JSON.stringify(state, null, 2)); return; }
    if (state.local_only) throw Error('This Store uses a local test identifier. Run connect --localhost --store-url to verify the local profile; public verification requires a real domain.');
    const response = await fetch(`https://${state.domain}/.well-known/ucp`, { redirect: 'error', signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw Error(`Public discovery returned HTTP ${response.status}; publish the prepared file first.`);
    const published = await response.json();
    const local = project.existingUcp.find(path => path.endsWith('/.well-known/ucp'));
    if (!local || JSON.stringify(published) !== JSON.stringify(JSON.parse(readFileSync(local, 'utf8')))) throw Error('Public UCP differs from the local prepared document.');
    console.log('Public UCP matches prepared document. Ownership and runtime protection require authenticated service checks.');
    return;
  }
  if (command === 'disconnect') {
    const state = readConfig(root);
    if(stateDirectory(root)===MANAGED_STATE && state) {
      const release=lockProject(root,MANAGED_STATE);
      try {
        const result=await disconnectHTTP(root,state,{
          authenticate:async()=>{
            const path=sessionPath(root,state.api_url,state.domain);
            let auth=cachedSession(path);
            if(auth?.access_token) {
              // Validate the cached owner session without exposing its token.
              try {await request(state.api_url,'/api/commerce/auth/me',{token:auth.access_token});return auth;}
              catch(error){if(!/^(401|403)\b/.test(String(error.message)))throw error;}
            }
            auth=await authenticate(state.api_url,options);
            atomicJSON(path,{...auth,expires_at:Date.now()+Math.max(0,Number(auth.expires_in||0)-60)*1000});return auth;
          },request,
          stopCompose:path=>runProcess('docker',['compose','-f',path,'stop','--timeout','30','auteric-runtime'],root),
        });
        console.log('Auteric disconnected. Generated connection files were removed; edited files and persistent audit/state were preserved under auteric or the existing runtime volume.');
        return result;
      } finally {release();}
    }
    if (!state?.store_id || !state?.api_url) throw Error('No connected Auteric Store was found in this project.');
    const authFile = sessionPath(root, state.api_url, state.domain);
    let auth = cachedSession(authFile);
    if (!auth?.access_token) {
      auth = await authenticate(state.api_url, options);
      atomicJSON(authFile, { ...auth, expires_at: Date.now() + Math.max(0, Number(auth.expires_in || 0) - 60) * 1000 });
    }
    await request(state.api_url, `/api/commerce/stores/${encodeURIComponent(state.store_id)}/agent-access`, {
      method: 'PUT', token: auth.access_token, body: { enabled: false },
    });
    const runtimeInstallation = state.runtime_enrollment ? state.installation_id : state.sidecar_installation_id;
    if (runtimeInstallation) {
      await request(state.api_url, `/api/commerce/stores/${encodeURIComponent(state.store_id)}/installations/${encodeURIComponent(runtimeInstallation)}/revoke`, {
        method: 'POST', token: auth.access_token,
      });
      if (state.runtime_enrollment) await request(state.api_url, `/api/commerce/stores/${encodeURIComponent(state.store_id)}/runtime-enrollment/${encodeURIComponent(runtimeInstallation)}`, {
        method: 'DELETE', token: auth.access_token,
      });
    }
    state.status = 'disconnected';
    state.disconnected_at = new Date().toISOString();
    state.public_discovery_verified = false;
    atomicJSON(configPath(root), state);
    connectionStatus(root, { phase: 'disconnect', status: 'disconnected', outcome: 'disconnected', production_ready: false });
    const localStop = state.sidecar_bundle ? join(state.sidecar_bundle, 'disconnect.sh') : null;
    console.log('Agent Access is disabled and the sidecar installation is revoked. Repository files and durable audit data were preserved.');
    if (localStop && existsSync(localStop)) console.log(`Stop the local containers with: ${localStop}`);
    return state;
  }
  console.log('Custom Connect: one model-led request; current coding model scans, writes auteric/.state/adapter-plan.json, resumes the same command, and runs local acceptance.');
  console.log('Local acceptance: connect --local-acceptance --runtime-source PATH [--runtime-commit SHA] [--python PATH]. Uses one generic Docker image; no merchant SDK.');
  console.log('HTTP mapping: --mapping openapi.json --deployment deployment.json --environment staging --test-origin http://127.0.0.1:PORT --test-query QUERY --test-currency USD. Existing outbound connector setup: --legacy-connector.');
  console.log('Usage: auteric connect [--domain store.example.com] [--localhost] [--local-storefront] [--api-url http://127.0.0.1:8100] [--store-url http://127.0.0.1:5500] [--backend-url http://127.0.0.1:3001] [--backend services/api] [--frontend apps/web] [--dry-run] [--no-agent] [--agent auto|codex|claude|cursor|copilot|none]');
  console.log('GitHub shortcut: npx --yes github:auteric-ai/auteric-kit --localhost --store-url http://127.0.0.1:5500');
  console.log('Also: auteric inspect | inventory [path] | bind [path] [--backend dir] | validate [path] | verify [path] [--port N] | connector | connect-status | login | status | verify | doctor | disconnect | logout');
  console.log('Progress: §18-style lines go to stderr by default; --quiet suppresses them, --json emits structured events.');
}

// Direct execution (`node src/cli.js ...`) behaves like the packaged binary.
if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  run(process.argv.slice(2)).then(result => {
    const completeIntegration = new Set(['locally_tested', 'native_http_verified']);
    if (result?.binding === 'pending' || result?.acceptance === 'incomplete' ||
        (result?.integration && (!completeIntegration.has(result.integration) || result.status === 'local_discovery_pending' || result.status === 'discovery_prepared'))) process.exitCode = 2;
  }).catch(error => {
    console.error(`Auteric: ${error.message}`);
    process.exitCode = 1;
  });
}
