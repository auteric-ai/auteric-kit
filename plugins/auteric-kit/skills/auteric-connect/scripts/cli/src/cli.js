import { sdk } from './sdk.js';
import { createHash, randomBytes } from 'node:crypto';
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const PROD_API = 'https://control.auteric.com';
const LOCAL_API = 'http://127.0.0.1:8100';

function parse(argv) {
  const [command = 'help', ...tail] = argv;
  const options = {};
  for (let i = 0; i < tail.length; i++) {
    const arg = tail[i];
    if (!arg.startsWith('--')) throw Error(`Unknown argument: ${arg}`);
    const [name, value] = arg.slice(2).split('=', 2);
    if (['localhost', 'dry-run', 'yes', 'no-browser'].includes(name)) {
      options[name] = true;
    } else if (['api-url', 'domain', 'agent', 'store-url', 'backend', 'frontend'].includes(name)) {
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
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw Error('--store-url must be a plain HTTP loopback origin, for example http://127.0.0.1:5500');
  }
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
  return { api: nodeApi || hasPythonApi, frontend: nodeUi || hasStatic };
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
    console.log(`Enter this one-time code in the browser: ${session.user_code}`);
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

function configPath(root) { return join(root, '.auteric', 'config.json'); }
function readConfig(root) { try { return JSON.parse(readFileSync(configPath(root), 'utf8')); } catch { return null; } }

async function connect(root, options) {
  const base = apiUrl(options);
  const storeUrl = options['store-url'] ? localStoreUrl(options['store-url']) : null;
  if (storeUrl && !options.localhost) throw Error('--store-url is only available with --localhost');
  const layout = resolveProjectLayout(root, options);
  const project = inspect(layout.frontend);
  const localOnly = Boolean(options.localhost && !options.domain);
  const domain = localOnly ? localTestDomain(root) : domainName(options.domain);
  if (project.existingUcp.length && readConfig(root)?.domain !== domain)
    throw Error(`UCP already exists: ${project.existingUcp.join(', ')}. Review before connecting.`);
  const agent = options.agent || project.agents[0] || 'none';
  if (!['codex', 'claude', 'cursor', 'none', 'auto'].includes(agent)) throw Error('Use --agent codex|claude|cursor|auto|none');
  console.log(`Store: ${domain} | Frontend: ${layout.frontendRelative} | Backend: ${layout.backendRelative} | Framework: ${project.framework} | Detected agent: ${agent}`);
  if (localOnly) console.log('This is a local test identifier, not a public domain or ownership proof.');
  console.log('Connect installs local skills, inventories every canonical capability, prepares supported adapters and tests them in the selected sandbox.');
  console.log('Candidate commerce libraries:', project.catalogCandidate.join(', ') || 'none detected');
  if (options['dry-run']) { console.log('Dry run: no authentication, store creation or file changes.'); return; }
  const prepared = await sdk('prepare', layout.backend, { agent: agent === 'claude' ? 'claude-code' : agent === 'auto' ? 'codex' : agent }, { install: true });
  console.log(`Inspected ${prepared.inventory.files_inspected} backend files. Capability report: ${join(layout.backend, '.auteric/capabilities.json')}`);
  const auth = await authenticate(base, options);
  console.log(`Signed in as ${auth.user.email} (${auth.user.organization})`);
  const stores = await request(base, '/api/commerce/stores', { token: auth.access_token });
  let store = stores.find(item => item.domain === domain);
  if (!store) {
    store = await request(base, '/api/commerce/stores', { method: 'POST', token: auth.access_token,
      body: { domain, name: domain, platform: 'custom', environment: options.localhost ? 'sandbox' : 'production' } });
  }
  const state = { discovery_digest: readConfig(root)?.discovery_digest, api_url: base, domain, store_id: store.id, framework: project.framework, backend_dir: layout.backendRelative, frontend_dir: layout.frontendRelative, mode: options.localhost ? 'local' : 'cloud', local_only: localOnly, status: 'store_registered' };
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  writeFileSync(configPath(root), JSON.stringify(state, null, 2) + '\n', { mode: 0o644 });
  const validation = await sdk('connect', layout.backend, { api_url: base, store_id: store.id, token: auth.access_token,
    environment: store.environment || (options.localhost ? 'sandbox' : 'production'), development: Boolean(options.localhost) });
  state.integration = validation.status;
  state.tested_operations = validation.tested_operations;
  state.credential_file = validation.credential_file;
  console.log(`Integration: ${validation.status}; tested operations: ${validation.tested_operations.join(', ') || 'none'}`);
  let discovery;
  try { discovery = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token }); }
  catch (error) { console.log(`UCP publication pending: ${error.message}`); }
  if (discovery?.document) {
    const result = prepareDiscovery(layout.frontend, project.framework, discovery.document, { previousDigest: readConfig(root)?.discovery_digest });
    state.discovery_digest = createHash('sha256').update(JSON.stringify(discovery.document, null, 2) + '\n').digest('hex');
    console.log(`Signed UCP prepared at ${result.path}.`);
    state.mcp_url = discovery.document.auteric_mcp?.endpoint;
    state.status = validation.status === 'locally_tested' ? 'capabilities_prepared' : validation.status;
    if (storeUrl) {
      const check = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/verify-local`, {
        method: 'POST', token: auth.access_token, body: { store_url: storeUrl },
      });
      if (check.local_verified && !check.public_domain_verified) {
        state.local_discovery_verified = true;
        console.log(`Local UCP verified at ${check.url}. Public ownership and runtime protection remain unverified.`);
      }
    }
  }
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  writeFileSync(configPath(root), JSON.stringify(state, null, 2) + '\n', { mode: 0o644 });
  try {
    await request(base, '/api/commerce/cli/complete', { method: 'POST', token: auth.access_token,
      body: { request_id: auth.request_id, store_id: store.id } });
  } catch (error) {
    if (!/^404\b/.test(error.message)) throw error;
    console.log('This control plane does not support automatic dashboard handoff yet. Open the dashboard link below.');
  }
  console.log(`Store created or resumed: ${store.id}. Local configuration: ${configPath(root)}`);
  console.log('Public publication and production verification are pending. Push and publish require your approval.');
  if (validation.credential_file) console.log(`Start the persistent local connector using the same CLI entrypoint: node ${process.argv[1]} connector`);
  console.log(`Your store dashboard: ${base}/console?store=${encodeURIComponent(store.id)}`);
  if (options.localhost) console.log('Local test mode: signatures from a development key and localhost URLs are not production trust or HTTPS merchant discovery.');
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
  if (command === 'connect') return connect(root, options);
  if (command === 'login') { const auth = await authenticate(apiUrl(options), options); console.log(`Signed in as ${auth.user.email}. This session is held only for this command; use connect to register a store.`); return; }
  if (command === 'logout') { console.log('No persistent CLI credential is stored. Browser sessions are managed in the Auteric console.'); return; }
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
  console.log('Usage: auteric connect [--domain store.example.com] [--localhost] [--api-url http://127.0.0.1:8100] [--store-url http://127.0.0.1:5500] [--backend services/api] [--frontend apps/web] [--dry-run] [--agent auto|codex|claude|cursor|none]');
  console.log('Also: auteric inspect | connector | login | status | verify | doctor | disconnect | logout');
}
