import { prepareWithAgent } from './agent.js';
import { atomicJSON, journal, lockProject, sessionPath, cachedSession, projectDigest, readJSON } from './workflow.js';
import { sdk } from './sdk.js';
import { createHash, randomBytes } from 'node:crypto';
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync, unlinkSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const PROD_API = 'https://control.auteric.com';
const LOCAL_API = 'http://127.0.0.1:8100';

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
    if (!arg.startsWith('--')) throw Error(`Unknown argument: ${arg}`);
    const [name, value] = arg.slice(2).split('=', 2);
    if (['localhost', 'dry-run', 'yes', 'no-browser', 'serve', 'no-agent'].includes(name)) {
      options[name] = true;
    } else if (['api-url', 'domain', 'agent', 'store-url', 'backend', 'frontend', 'backend-url'].includes(name)) {
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

export function inspect(root) {
  const isFile = file => existsSync(join(root, file));
  const packageData = isFile('package.json') ? JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')) : {};
  const deps = { ...packageData.dependencies, ...packageData.devDependencies };
  const framework = deps.next ? 'next' : deps.nuxt ? 'nuxt' : deps.vite ? 'vite' : deps.express ? 'express' : isFile('index.html') ? 'static' : 'custom';
  const agents = ['codex', 'claude', 'cursor'].filter(agent => isFile(agent === 'cursor' ? '.cursor' : agent === 'claude' ? '.claude' : '.codex') || executable(agent === 'cursor' ? 'cursor-agent' : agent));
  return { framework, agents, catalogCandidate: Object.keys(deps).filter(dep => /commerce|shopify|medusa|stripe/.test(dep)),
    existingUcp: [join(root, '.well-known/ucp'), join(root, 'public/.well-known/ucp')].filter(existsSync) };
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
  let response;
  try {
    response = await fetch(base + path, { method, headers: {
      ...(body ? { 'content-type': 'application/json' } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(method !== 'GET' && token ? { 'x-auteric-console': '1' } : {}),
    }, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(12000), redirect: 'error' });
  } catch { throw Error(`Cannot contact ${base}. Start the Commerce service or check its URL.`); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(`${response.status} ${typeof data.detail === 'string' ? data.detail : 'Service request failed'}`);
  return data;
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
  const session = await request(base, '/api/commerce/cli/start', { method: 'POST', body: { challenge, state } });
  const link = new URL(session.authorization_url);
  if (link.origin !== base || link.pathname !== '/cli/authorize') throw Error('Unexpected authorization URL from control plane');
  console.log(`Open this Auteric sign-in page:\n${link.href}`);
  if (session.user_code) {
    if (!/^\d{4}-\d{4}$/.test(session.user_code)) throw Error('Invalid pairing code from control plane');
    console.log(`Merchant ID (one-time pairing code): ${session.user_code}`);
  } else {
    console.log('This control plane has not enabled CLI pairing codes yet.');
  }
  if (!options['no-browser']) openBrowser(link.href);
  const deadline = Math.min(session.expires_at * 1000, Date.now() + 300000);
  while (Date.now() < deadline) {
    await delay(Math.max(2000, Number(session.interval || 2) * 1000));
    const reply = await request(base, '/api/commerce/cli/poll', {
      method: 'POST', body: { request_id: session.request_id, state, verifier },
    });
    if (reply.status === 'authorized') return { ...reply, request_id: session.request_id };
  }
  throw Error('Browser authorization expired. Run connect again.');
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

function configPath(root) { return join(root, '.auteric', 'config.json'); }
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

async function connect(root, options) {
  const base = apiUrl(options);
  const storeUrl = options['store-url'] ? localStoreUrl(options['store-url']) : null;
  if (storeUrl && !options.localhost) throw Error('--store-url is only available with --localhost');
  const layout = resolveProjectLayout(root, options);
  const project = inspect(layout.frontend);
  const localOnly = Boolean(options.localhost && !options.domain);
  const domain = localOnly ? localTestDomain(root) : domainName(options.domain);
  const backendUrl = options['backend-url']
    ? (options.localhost ? localStoreUrl(options['backend-url']) : apiUrl({ 'api-url': options['backend-url'] }))
    : storeUrl || (localOnly ? null : `https://${domain}`);
  const probeUrl = options.localhost ? backendUrl : null;
  if (project.existingUcp.length && readConfig(root)?.domain !== domain)
    throw Error(`UCP already exists: ${project.existingUcp.join(', ')}. Review before connecting.`);
  const agent = options['no-agent'] ? 'none' : options.agent || 'auto';
  if (!['codex', 'claude', 'cursor', 'copilot', 'none', 'auto'].includes(agent)) throw Error('Use --agent codex|claude|cursor|auto|none');
  console.log(`Store: ${domain} | Frontend: ${layout.frontendRelative} | Backend: ${layout.backendRelative} | Framework: ${project.framework} | Instructions: ${agent === 'auto' ? 'Codex/Copilot, Claude, Cursor' : agent}`);
  if (localOnly) console.log('This is a local test identifier, not a public domain or ownership proof.');
  console.log('Connect inventories the full API surface, selects supported shopping capabilities, prepares their adapters and tests them in the selected sandbox.');
  console.log('Candidate commerce libraries:', project.catalogCandidate.join(', ') || 'none detected');
  if (options['dry-run']) { console.log('Dry run: no authentication, store creation or file changes.'); return; }
  journal(root, 'inspection', 'running');
  let prepared = await sdk('prepare', layout.backend, { agent: agent === 'claude' ? 'claude-code' : agent === 'copilot' ? 'codex' : agent,
    store_url: probeUrl, instructions_root: root }, { install: true });
  const conflicts = (prepared.skill || []).filter(item => item.conflict).map(item => item.client);
  if (conflicts.length) console.log(`Existing assistant instructions preserved for: ${conflicts.join(', ')}. Review these files manually.`);
  const summary = prepared.inventory.inventory_summary || {};
  console.log(`Inspected ${prepared.inventory.files_inspected} backend files and inventoried ${summary.api_endpoints ?? summary.total_endpoints ?? 0} APIs plus ${summary.storefront_routes ?? 0} storefront routes; ${summary.tool_candidates ?? 0} are canonical shopping-tool candidates. Capability report: ${join(layout.backend, '.auteric/capabilities.json')}`);
  journal(root, 'inspection', 'complete');
  let assistantAttempted = false;
  if (!prepared.connector_prepared && agent !== 'none') {
    const assistant = await prepareWithAgent(root, layout.backend, backendUrl, agent);
    assistantAttempted = true;
    console.log(`Adapter preparation: ${assistant.status}`);
    prepared = await sdk('prepare', layout.backend, { agent: 'none', store_url: probeUrl, instructions_root: root });
  }
  if (!prepared.connector_prepared) {
    journal(root, 'adapter', 'implementation_required');
    for (const reason of prepared.inventory.connector_diagnostics || []) console.log(`Diagnosis: ${reason}`);
    console.log('Integration incomplete: no commerce connector is configured. The coding agent must trace the detected APIs, implement .auteric/connector.json and test its handlers before rerunning Connect. No new Store or UCP was created.');
    return { integration: 'implementation_required', tested_operations: [] };
  }
  journal(root, 'local_validation', 'running');
  let local = await sdk('local-check', layout.backend, { development: Boolean(options.localhost) });
  let pending = uncoveredOperations(prepared.inventory, local.prepared_operations);
  if (local.status === 'local_contract_passed' && pending.length && agent !== 'none' && !assistantAttempted) {
    const assistant = await prepareWithAgent(root, layout.backend, backendUrl, agent, { missingOperations: pending });
    assistantAttempted = true;
    console.log(`Capability completion: ${assistant.status}`);
    prepared = await sdk('prepare', layout.backend, { agent: 'none', store_url: probeUrl, instructions_root: root });
    local = await sdk('local-check', layout.backend, { development: Boolean(options.localhost) });
    pending = uncoveredOperations(prepared.inventory, local.prepared_operations);
  }
  journal(root, 'local_validation', local.status);
  if (pending.length) console.log(`Unconnected source candidates (not exposed as tools): ${pending.join(', ')}. Their business/session contracts still require an adapter.`);
  if (local.status !== 'local_contract_passed') {
    console.log(`Adapter validation: ${local.status}. See .auteric/local-validation.json; authentication has not started.`);
    return { integration: local.status, tested_operations: local.tested_operations || [] };
  }
  const authPath = sessionPath(root, base, domain);
  let auth = cachedSession(authPath);
  if (auth) {
    try { await request(base, '/api/commerce/auth/me', { token: auth.access_token }); }
    catch (error) { if (!/^(401|403)\b/.test(error.message)) throw error; unlinkSync(authPath); auth = null; }
  }
  journal(root, 'authentication', 'running');
  if (!auth) {
    auth = await authenticate(base, options);
    atomicJSON(authPath, { ...auth, expires_at: Date.now() + Math.max(0, Number(auth.expires_in || 0) - 60) * 1000 });
  }
  journal(root, 'authentication', 'complete');
  console.log(`Signed in as ${auth.user.email} (${auth.user.organization})`);
  const stores = await request(base, '/api/commerce/stores', { token: auth.access_token });
  const previous = readConfig(root);
  if (previous?.store_id && (previous.api_url !== base || previous.domain !== domain || !stores.some(item => item.id === previous.store_id && item.domain === domain)))
    throw Error('Saved Store does not belong to this account or control plane. No new Store was created.');
  let store = stores.find(item => item.domain === domain && (!previous?.store_id || item.id === previous.store_id));
  if (!store) {
    store = await request(base, '/api/commerce/stores', { method: 'POST', token: auth.access_token,
      body: { domain, name: domain, platform: 'custom', environment: options.localhost ? 'sandbox' : 'production' } });
  }
  const state = { ...previous, discovery_digest: readConfig(root)?.discovery_digest, api_url: base, domain, store_id: store.id, framework: project.framework, backend_dir: layout.backendRelative, frontend_dir: layout.frontendRelative, mode: options.localhost ? 'local' : 'cloud', local_only: localOnly, status: 'store_registered' };
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  atomicJSON(configPath(root), state);
  journal(root, 'runtime_validation', 'running');
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
    validation = await sdk('connect', layout.backend, { api_url: base, store_id: store.id, token: auth.access_token,
      environment: store.environment || (options.localhost ? 'sandbox' : 'production'), development: Boolean(options.localhost) });
  }
  state.project_digest = digest;
  state.unconnected_candidates = pending;
  journal(root, 'runtime_validation', validation.status);
  state.integration = validation.status;
  state.tested_operations = validation.tested_operations;
  state.credential_file = validation.credential_file;
  console.log(`Integration: ${validation.status}; tested operations: ${validation.tested_operations.join(', ') || 'none'}`);
  if (validation.status !== 'locally_tested') {
    atomicJSON(configPath(root), state);
    console.log('Setup is incomplete. No new UCP was generated. Inspect the validation report and finish the required adapter tests or production preparation.');
    return state;
  }
  atomicJSON(configPath(root), state);
  journal(root, 'discovery', 'running');
  let discovery;
  try { discovery = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token }); }
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
    if (!options.localhost || state.local_discovery_verified) {
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
  journal(root, 'discovery', state.local_discovery_verified ? 'local_verified' : 'pending');
  try {
    await request(base, '/api/commerce/cli/complete', { method: 'POST', token: auth.access_token,
      body: { request_id: auth.request_id, store_id: store.id } });
  } catch (error) {
    if (!/^404\b/.test(error.message)) throw error;
    console.log('This control plane does not support automatic dashboard handoff yet. Open the dashboard link below.');
  }
  console.log(`Store created or resumed: ${store.id}. Local configuration: ${configPath(root)}`);
  console.log('Public publication and production verification are pending.');
  if (validation.credential_file) console.log(`Start the persistent local connector using the same CLI entrypoint: node ${process.argv[1]} connector`);
  console.log(`Your store dashboard: ${base}/console?store=${encodeURIComponent(store.id)}`);
  if (options.localhost) console.log('Local test mode: signatures from a development key and localhost URLs are not production trust or HTTPS merchant discovery.');
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
    return sdk('serve', backend, { credential_file: state.credential_file });
  }
  if (command === 'connect') {
    if (options['dry-run']) return connect(root, options);
    const release = lockProject(root);
    try { return await connect(root, options); }
    catch (error) { journal(root, 'connection', 'failed'); throw error; }
    finally { release(); }
  }
  if (command === 'login') { const auth = await authenticate(apiUrl(options), options); console.log(`Signed in as ${auth.user.email}. This session is held only for this command; use connect to register a store.`); return; }
  if (command === 'logout') { const state = readConfig(root); if (state) { const path = sessionPath(root, state.api_url, state.domain); if (existsSync(path)) unlinkSync(path); } console.log('Project CLI session removed. Browser sessions and connector access are managed in the Auteric console.'); return; }
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
    throw Error('Disconnect is unavailable in this version. Disable agent access in Auteric Console; no repository files were deleted.');
  }
  console.log('Usage: auteric connect [--domain store.example.com] [--localhost] [--api-url http://127.0.0.1:8100] [--store-url http://127.0.0.1:5500] [--backend-url http://127.0.0.1:3001] [--backend services/api] [--frontend apps/web] [--dry-run] [--no-agent] [--agent auto|codex|claude|cursor|copilot|none]');
  console.log('GitHub shortcut: npx --yes github:auteric-ai/auteric-kit --localhost --store-url http://127.0.0.1:5500');
  console.log('Also: auteric inspect | connector | login | status | verify | doctor | disconnect | logout');
}
