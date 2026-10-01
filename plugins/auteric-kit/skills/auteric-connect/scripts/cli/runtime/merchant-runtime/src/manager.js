import { readFileSync, existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { join, dirname } from 'node:path';
import { randomBytes, createHash } from 'node:crypto';
import { validateConnection, validateMapping, digest } from './mapping.js';
import { atomicWrite } from './atomic.js';
import { bundledRelease } from './release.js';
import { moduleBinding } from './module-adapter.js';

export function validateBundle(bundle, connection, now = Date.now() / 1000) {
  if (bundle.schema !== 'auteric-runtime-bundle/v1' || bundle.installation_id !== connection.installation_id
      || bundle.mapping_digest !== connection.mapping_digest || digest(bundle.mapping) !== connection.mapping_digest
      || !Number.isFinite(bundle.expires_at) || bundle.expires_at <= now) throw Error('runtime configuration scope, digest or expiry mismatch');
  validateMapping(bundle.mapping);
  if(bundle.engine_profile && bundle.engine_profile!==process.env.AUTERIC_ENGINE_PROFILE) throw Error('runtime engine profile differs from the pinned SDK');
  const installation = bundle.sidecar?.installation;
  if (bundle.sidecar?.schema !== 'auteric-sidecar/v1' || installation?.installation_id !== connection.installation_id
      || installation?.store_id !== connection.store_id || installation?.environment !== connection.environment) throw Error('Sidecar configuration installation mismatch');
  const selected = bundle.mapping.operations.map(op => op.operation).sort();
  if (JSON.stringify(Object.keys(installation.manifest || {}).filter(op=>op!=='health').sort()) !== JSON.stringify(selected)
      || JSON.stringify(Object.keys(bundle.sidecar.integration?.profiles || {}).sort()) !== JSON.stringify(selected)) throw Error('Sidecar operation set differs from registered mapping');
  const policy = bundle.sidecar.operational?.policy;
  if (!policy || !Number.isFinite(policy.expires_at) || policy.expires_at < bundle.expires_at
      || policy.allowed_operations.some(op => !selected.includes(op))) throw Error('Sidecar policy scope or expiry mismatch');
  for (const op of selected) {
    const profile = bundle.sidecar.integration.profiles[op];
    if (profile.target?.base_url !== 'http://127.0.0.1:3101' || profile.target.auth_scheme !== 'bearer'
        || profile.target.credential_ref !== 'env:AUTERIC_BRIDGE_TOKEN' || profile.target.method !== 'POST'
        || profile.target.path !== `/invoke/${op}` || profile.integration_mode !== 'service_bridge') throw Error('Sidecar profile does not target the supported private Bridge');
    if(bundle.mapping.schema==='auteric-module-binding/v1' && profile.mapping_fingerprint!==bundle.mapping.adapter_fingerprint) throw Error('Sidecar adapter binding differs from enrolled adapter');
    if (profile.enabled && (!profile.verification || !Number.isFinite(profile.verification.expires_at)
        || profile.verification.expires_at < bundle.expires_at)) throw Error('Sidecar evidence expires before runtime bundle');
  }
  if (typeof bundle.discovery !== 'string' || Buffer.byteLength(bundle.discovery) > 65536) throw Error('exact service-issued discovery JSON required');
  const discovery = JSON.parse(bundle.discovery);
  if (!discovery || typeof discovery !== 'object' || !discovery.ucp) throw Error('service discovery document missing');
  return bundle;
}
export async function stopChild(child, timeout = 30000) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
    child.kill('SIGTERM');
  });
}

/** Configuration swaps drain requests through the public process boundary.
 * Existing Sidecar policy/evidence/ownership/replay checks remain authoritative.
 */
export async function manage(connection, {
  statePath, secretPath, discoveryPath, port = 7080, fetcher = fetch, spawnChild = spawn,
  secretStore, durableState, databaseUrl, adapterConnection = process.env.AUTERIC_ADAPTER_CONNECTION, clock = () => Date.now() / 1000, pollMs = 60000, drainMs = 30000, write = atomicWrite,
} = {}) {
  validateConnection(connection, { privateHosts: [new URL(connection.backend.origin).hostname] });
  const release = bundledRelease();
  if (connection.runtime_release !== release.version || connection.mapping.registry_digest !== release.registry_digest) throw Error('runtime release or registry incompatibility');
  if (!statePath || (!secretPath && !secretStore) || !discoveryPath) throw Error('persistent state, service secret file and discovery mount required');
  const isModule=connection.mapping.schema==='auteric-module-binding/v1';
  if(isModule && (!adapterConnection || digest(await moduleBinding(adapterConnection))!==connection.mapping_digest))throw Error('deployed adapter differs from enrolled binding');
  const serviceRoot = `${connection.control_origin}/api/commerce/stores/${encodeURIComponent(connection.store_id)}/runtime-enrollment`;
  let children = [], bundle, health = 'deployment_pending', closed = false, serving = false;
  let retries = 0, timer, activeToken, running, stopping, inFlight = 0, bridgeCredential;
  const verificationPath = join(statePath, 'verification-state.json');
  const publishDiscovery = next => {
    write(discoveryPath,next.discovery,0o644);
    write(join(dirname(discoveryPath),'.auteric-owner.json'),JSON.stringify({
      installation_id:connection.installation_id,digest:'sha256:'+createHash('sha256').update(next.discovery).digest('hex'),
    }));
  };
  let uncertain = durableState ? await durableState.read() : existsSync(verificationPath) ? JSON.parse(readFileSync(verificationPath, 'utf8')) : null;
  const saveState = async value => durableState ? durableState.save(value) : write(verificationPath,JSON.stringify(value));
  const saveToken = async value => secretStore ? secretStore.save(value) : write(secretPath,JSON.stringify(value));
  const token = async () => {
    const value = secretStore ? await secretStore.read() : JSON.parse(readFileSync(secretPath, 'utf8'));
    if (value.installation_id !== connection.installation_id || !Number.isFinite(value.expires_at)
        || value.expires_at <= clock() || typeof value.token !== 'string' || value.token.length < 32) {
      const error = Error('runtime service credential invalid or expired'); error.status = 401; throw error;
    }
    return value;
  };
  const api = async (method, path, requestId) => {
    const credential = await token(); activeToken = credential;
    const response = await fetcher(serviceRoot + path, {
      method, headers: { authorization: 'Bearer ' + credential.token, 'content-type': 'application/json',...(requestId?{'idempotency-key':requestId}:{}) },
      redirect: 'error', signal: AbortSignal.timeout(path==='/verification'?180000:15000),
    });
    if (!response.ok) {
      const error = Error(`Control ${method} ${path} rejected: ${response.status}`); error.status = response.status; throw error;
    }
    return response.json();
  };
  const stop = () => {
    serving = false;
    if (stopping) return stopping;
    stopping = (async () => {
      const deadline = Date.now() + drainMs;
      while (inFlight && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
      // Drain is bounded. A timed-out write is never retried by this supervisor.
      const retired = children; children = [];
      for (const child of retired) await stopChild(child, drainMs);
    })().finally(() => { stopping = undefined; });
    return stopping;
  };
  const start = async next => {
    await stop();
    if (closed) return;
    if(isModule && digest(await moduleBinding(adapterConnection))!==connection.mapping_digest)throw Error('deployed adapter changed after enrollment');
    const bridgeToken = randomBytes(40).toString('base64url'); bridgeCredential = bridgeToken;
    const generation = join(statePath, 'config-' + randomBytes(8).toString('hex'));
    write(join(generation, 'mapping.json'), JSON.stringify(next.mapping));
    const sidecar=structuredClone(next.sidecar);
    for(const key of ['execution_store','audit_store']) {
      const value=sidecar.operational?.[key];
      if(typeof value==='string' && /^sqlite:\/data\/[a-zA-Z0-9.-]+$/.test(value))sidecar.operational[key]='sqlite:'+join(statePath,value.slice('sqlite:/data/'.length));
    }
    write(join(generation, 'sidecar.json'), JSON.stringify(sidecar));
    const env = { ...process.env, AUTERIC_BRIDGE_TOKEN: bridgeToken, AUTERIC_MAPPING: join(generation, 'mapping.json'),
      AUTERIC_SIDECAR_CONFIG: join(generation, 'sidecar.json'), AUTERIC_MERCHANT_ORIGIN: connection.backend.origin,
      AUTERIC_INSTALLATION_ID: connection.installation_id, ...(isModule ? {AUTERIC_ADAPTER_CONNECTION:adapterConnection,AUTERIC_BINDING_DIGEST:next.mapping.adapter_fingerprint} : {}), AUTERIC_SIDECAR_GATEWAY_TOKEN: activeToken.token, AUTERIC_SIDECAR_DATABASE_URL:databaseUrl, AUTERIC_BRIDGE_STATE: join(statePath, 'bridge.sqlite'), ...(databaseUrl?{AUTERIC_STATE_DATABASE_URL:databaseUrl}:{}) };
    children = [spawnChild(process.execPath, [new URL('./bridge-cli.js', import.meta.url).pathname], { env, stdio: 'inherit' }),
      spawnChild('python3', ['-m', 'auteric_merchant.sidecar_app', '--host', '127.0.0.1', '--port', '7070'], { env, stdio: 'inherit' })];
    for (const child of children) {
      const failed = () => { if (!closed && children.includes(child)) { health = 'degraded'; void stop(); } };
      child.once('exit', failed); child.once('error', failed);
    }
    bundle = {...next,service_token:activeToken.token};
    publishDiscovery(next);
    health = 'deployed_unverified'; serving = true;
  };
  const reportPassed = next => next.verification_state?.state==='passed';
  const maintenance = async () => {
    try {
      const credential = await token();
      if (credential.enrollment === true) {
        let exchanged; try {exchanged=await api('POST','/exchange');}
        catch(error){if(error.status!==401 && error.status!==409)throw error;exchanged=await api('GET','/exchange-result');}
        await saveToken(exchanged);
      }
      const received = await api('GET', '/config');
      let next;
      try { next = validateBundle(received, connection, clock()); }
      catch (error) { error.status = 409; throw error; }
      if (uncertain) {
        if (uncertain.request_id) {
          const observed=await api('GET','/verification');
          if(observed.run_id===uncertain.request_id && ['passed','failed'].includes(observed.state)) {
            await saveState(null); uncertain=null;
          }
        }
        if(uncertain && typeof uncertain.run_id === 'string' && uncertain.run_id
            && next.verification_state?.run_id === uncertain.run_id && next.verification_state?.state === 'reconciled') {
          await saveState(null); uncertain = null;
        } else if(uncertain) { const error = Error('Uncertain verification requires authoritative reconciliation'); error.status = 409; throw error; }
      }
      if (closed) return;
      if (!bundle || activeToken.token!==bundle.service_token || digest(next.sidecar) !== digest(bundle.sidecar) || children.length !== 2
          || children.some(c => c.exitCode !== null || c.signalCode !== null)) await start(next);
      else { bundle = {...next,service_token:activeToken.token}; publishDiscovery(next); }
      if(next.verification_required) {
        let available=false;
        for(let i=0;i<100 && !closed;i++){
          try{available=(await fetcher('http://127.0.0.1:7070/health/live',{signal:AbortSignal.timeout(2000)})).ok && (await fetcher('http://127.0.0.1:3101/health/ready',{headers:{authorization:'Bearer '+bridgeCredential},signal:AbortSignal.timeout(2000)})).ok;}catch{}
          if(available)break;await new Promise(resolve=>setTimeout(resolve,100));
        }
        if(!available)throw Error('runtime processes are not ready for bootstrap verification');
      }
      if (next.domain_refresh_required) await api('POST', '/refresh-domain');
      // An uncertain run remains blocked across restart in Control's persistent job ledger.
      // GET config does not mint/extend verification and must never clear it.
      if (next.verification_required && health !== 'blocked') {
        let report;
        try {
          const requestId=randomBytes(16).toString('hex');
          uncertain={request_id:requestId,state:'pending'};
          await saveState(uncertain);
          report = await api('POST', '/verification',requestId);
          if (typeof report.run_id !== 'string' || !['passed', 'failed', 'uncertain'].includes(report.state)) throw Error('verification response invalid');
        }
        catch (error) {
          // A lost response can follow a committed merchant mutation. Stop
          // serving before persistence, including when the state disk fails.
          uncertain = { ...uncertain, run_id: null, state: 'uncertain', reason: 'verification_response_unknown' };
          health = 'blocked'; await stop();
          await saveState(uncertain);
          throw error;
        }
        if (report.state !== 'uncertain') { await saveState(null); uncertain=null; }
        if (report.state === 'uncertain') { uncertain = { run_id: report.run_id, state: 'uncertain' }; health = 'blocked'; await stop(); await saveState(uncertain); }
        else if (report.state !== 'passed') health = 'deployed_unverified';
      }
      if (activeToken.bootstrap && !uncertain && reportPassed(next)) await saveToken(await api('POST','/promote'));
      if (!activeToken.bootstrap && activeToken.expires_at - clock() < 3600) await saveToken(await api('POST', '/rotate'));
      if(next.verification_state?.state==='passed' && !activeToken.bootstrap && Object.values(next.sidecar.integration.profiles).every(p=>p.enabled))health='verified';
      retries = 0;
    } catch (error) {
      health = uncertain || error.status === 409 ? 'blocked' : 'degraded'; retries = Math.min(retries + 1, 6);
      if ([401, 403, 409].includes(error.status)) await stop();
    }
    if (bundle?.expires_at <= clock()) { health = 'degraded'; await stop(); }
  };
  const poll = () => {
    if (closed) return Promise.resolve();
    if (running) return running;
    running = maintenance().finally(() => { running = undefined; });
    return running;
  };
  const schedule = () => {
    if (!closed) timer = setTimeout(async () => { await poll(); schedule(); }, (retries ? Math.min(pollMs, 1000 * 2 ** retries) : pollMs) + Math.floor(Math.random() * 500));
  };
  const healthyChildren = () => children.length === 2 && children.every(c => c.exitCode === null && c.signalCode === null);
  const usable = () => !closed && serving && bundle && bundle.expires_at > clock() && healthyChildren();
  const server = createServer((req, res) => void (async () => {
    if (req.method === 'GET' && req.url === '/health/live') {
      res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"status":"live"}'); return;
    }
    if (req.method === 'GET' && req.url === '/health/ready') {
      let ready = false;
      if (usable() && health !== 'blocked') {
        try {
          await token();
          ready = (await fetcher('http://127.0.0.1:7070/health/'+(bundle.bootstrap?'live':'ready'), { signal: AbortSignal.timeout(3000) })).ok
            && (await fetcher('http://127.0.0.1:3101/health/ready', {
              headers: { authorization: 'Bearer ' + bridgeCredential }, signal: AbortSignal.timeout(3000),
            })).ok;
        } catch { /* unavailable credential/child -> not ready */ }
      }
      res.writeHead(ready ? 200 : 503, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ status: ready ? 'ready' : 'not_ready', maintenance: health })); return;
    }
    if (req.method === 'GET' && req.url === '/.well-known/ucp' && usable()) {
      res.writeHead(200, { 'content-type': 'application/json' }); res.end(bundle.discovery); return;
    }
    if (!['/api/auteric/v1/','/api/auteric/agent/v1/'].some(prefix=>req.url?.startsWith(prefix)) || !usable() || health === 'blocked') { res.writeHead(503); res.end(); return; }
    await token();
    inFlight++;
    try {
      let size = 0; const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1048576) { res.writeHead(413); res.end(); return; }
        chunks.push(chunk);
      }
      // Preserve the raw signed request target and body. URL normalization is
      // rejected before the proxy, never used to transform a signed operation.
      if (/[\\]/.test(req.url) || /(?:^|\/)\.{1,2}(?:\/|$)/.test(req.url.split('?')[0]) || /%2e|%2f|%5c/i.test(req.url.split('?')[0])) { res.writeHead(400); res.end(); return; }
      const headers = { ...req.headers }; delete headers.host; delete headers.connection; delete headers['transfer-encoding'];
      const response = await fetcher('http://127.0.0.1:7070' + req.url, {
        method: req.method, headers, ...(['GET', 'HEAD'].includes(req.method) ? {} : { body: Buffer.concat(chunks) }),
        redirect: 'error', signal: AbortSignal.timeout(drainMs),
      });
      res.writeHead(response.status, { 'content-type': response.headers.get('content-type') || 'application/json' });
      res.end(Buffer.from(await response.arrayBuffer()));
    } finally { inFlight--; }
  })().catch(() => { if (!res.headersSent) res.writeHead(502); res.end(); }));
  server.requestTimeout = 35000; server.headersTimeout = 10000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '0.0.0.0', resolve); });
  await poll(); schedule();
  const expiryTimer = setInterval(() => { if (bundle && bundle.expires_at <= clock()) { health = 'degraded'; void stop(); } }, 1000);
  return { server, poll, close: async () => {
    closed = true; serving = false; clearTimeout(timer); clearInterval(expiryTimer);
    if (running) await running;
    await stop(); await new Promise(resolve => server.close(resolve));
  } };
}
