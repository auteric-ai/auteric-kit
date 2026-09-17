import { createHash, randomBytes } from 'node:crypto';
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline/promises';
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
    } else if (['api-url', 'domain', 'agent', 'store-url'].includes(name)) {
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
  const framework = deps.next ? 'next' : deps.nuxt ? 'nuxt' : deps.express ? 'express' : isFile('index.html') ? 'static' : 'custom';
  const agents = ['codex', 'claude', 'cursor'].filter(agent => isFile(agent === 'cursor' ? '.cursor' : agent === 'claude' ? '.claude' : '.codex') || executable(agent === 'cursor' ? 'cursor-agent' : agent));
  return { framework, agents, catalogCandidate: Object.keys(deps).filter(dep => /commerce|shopify|medusa|stripe/.test(dep)),
    existingUcp: [join(root, '.well-known/ucp'), join(root, 'public/.well-known/ucp')].filter(existsSync) };
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
    if (reply.status === 'authorized') return reply;
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
  return join(root, ['next', 'nuxt'].includes(framework) || (framework !== 'static' && existsSync(join(root, 'public'))) ? 'public/.well-known/ucp' : '.well-known/ucp');
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
    if (readFileSync(path, 'utf8') !== contents) throw Error(`Existing UCP profile at ${path} differs. Reconcile it; no file was overwritten.`);
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
  const project = inspect(root);
  const localOnly = Boolean(options.localhost && !options.domain);
  const domain = localOnly ? localTestDomain(root) : domainName(options.domain);
  if (project.existingUcp.length && readConfig(root)?.domain !== domain)
    throw Error(`UCP already exists: ${project.existingUcp.join(', ')}. Review before connecting.`);
  const agent = options.agent || project.agents[0] || 'none';
  if (!['codex', 'claude', 'cursor', 'none', 'auto'].includes(agent)) throw Error('Use --agent codex|claude|cursor|auto|none');
  console.log(`Store: ${domain} | Framework: ${project.framework} | Detected agent: ${agent}`);
  if (localOnly) console.log('This is a local test identifier, not a public domain or ownership proof.');
  console.log('The CLI does not install coding-agent adapters or verify commerce APIs yet.');
  console.log('Candidate commerce libraries:', project.catalogCandidate.join(', ') || 'none detected');
  if (options['dry-run']) { console.log('Dry run: no authentication, store creation or file changes.'); return; }
  const auth = await authenticate(base, options);
  console.log(`Signed in as ${auth.user.email} (${auth.user.organization})`);
  const stores = await request(base, '/api/commerce/stores', { token: auth.access_token });
  let store = stores.find(item => item.domain === domain);
  if (!store) {
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    let approval;
    try { approval = options.yes ? 'yes' : await rl.question(`Create a pending Auteric store for ${domain} in ${auth.user.organization}? [y/N] `); }
    finally { rl.close(); }
    if (!['yes', 'y'].includes(approval.trim().toLowerCase())) { console.log('Store creation cancelled.'); return; }
    store = await request(base, '/api/commerce/stores', { method: 'POST', token: auth.access_token,
      body: { domain, name: domain, platform: 'custom', environment: options.localhost ? 'sandbox' : 'production' } });
  }
  const state = { api_url: base, domain, store_id: store.id, framework: project.framework, mode: options.localhost ? 'local' : 'cloud', local_only: localOnly, status: 'store_registered' };
  let discovery;
  try { discovery = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/discovery`, { token: auth.access_token }); }
  catch (error) { console.log(`UCP publication pending: ${error.message}`); }
  if (discovery?.document) {
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    let approval;
    try { approval = options.yes ? 'yes' : await rl.question(`Prepare signed UCP at ${destination(root, project.framework)}? [y/N] `); }
    finally { rl.close(); }
    if (['y', 'yes'].includes(approval.trim().toLowerCase())) {
      const result = prepareDiscovery(root, project.framework, discovery.document);
      console.log(`Prepared ${result.path}. Review, commit and publish it on the store hostname.`);
      state.status = 'discovery_prepared';
      if (storeUrl) {
        const check = await request(base, `/api/commerce/stores/${encodeURIComponent(store.id)}/verify-local`, {
          method: 'POST', token: auth.access_token, body: { store_url: storeUrl },
        });
        if (check.local_verified && !check.public_domain_verified) {
          state.status = 'locally_verified';
          console.log(`Local UCP verified at ${check.url}. Public ownership and runtime protection remain unverified.`);
        }
      }
    }
  }
  mkdirSync(resolve(configPath(root), '..'), { recursive: true });
  writeFileSync(configPath(root), JSON.stringify(state, null, 2) + '\n', { mode: 0o644 });
  console.log(`Store created or resumed: ${store.id}. Local configuration: ${configPath(root)}`);
  console.log('Ownership and capabilities remain unverified until public publication and successful connection tests.');
  console.log(`Control plane: ${base}/console`);
  if (options.localhost) console.log('Local test mode: signatures from a development key and localhost URLs are not production trust or HTTPS merchant discovery.');
}

export async function run(argv, root = process.cwd()) {
  const { command, options } = parse(argv);
  if (command === 'connect') return connect(root, options);
  if (command === 'login') { const auth = await authenticate(apiUrl(options), options); console.log(`Signed in as ${auth.user.email}. This session is held only for this command; use connect to register a store.`); return; }
  if (command === 'logout') { console.log('No persistent CLI credential is stored. Browser sessions are managed in the Auteric console.'); return; }
  if (command === 'status' || command === 'doctor' || command === 'verify') {
    const state = readConfig(root);
    const project = inspect(root);
    if (command === 'doctor') {
      console.log(JSON.stringify({ node: process.version, framework: project.framework, agents: project.agents, existingUcp: project.existingUcp, api_url: apiUrl(options) }, null, 2));
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
  console.log('Usage: auteric connect [--domain store.example.com] [--localhost] [--api-url http://127.0.0.1:8100] [--store-url http://127.0.0.1:5500] [--dry-run] [--agent auto|codex|claude|cursor|none]');
  console.log('Also: auteric login | status | verify | doctor | disconnect | logout');
}
