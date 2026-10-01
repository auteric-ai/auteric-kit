import { bridgeStore } from './bridge-store.js';
import { createHash } from 'node:crypto';
import { backendOrigin } from './mapping.js';
export class BridgeError extends Error {
  constructor(code, status, message = code) { super(message); this.code = code; this.status = status; }
}
export const fail = (code, status, message) => { throw new BridgeError(code, status, message); };
const PREFIX = { product: 'prod', variant: 'var', cart: 'cart', line: 'line', checkout: 'chk', order: 'ord', buyer: 'buyer', shipping_option: 'opt' };

// Translation metadata only. Business transactions and session issuance belong to the merchant.
export function translationHelpers({ origin, installationId, statePath, privateHosts = [], fetcher = fetch,
  timeoutMs = 10000, databaseUrl, store, sessionConfig, errorMap = {} }) {
  if (!installationId || (!statePath && !databaseUrl && !store)) throw Error('installation and durable bridge state required');
  const base = backendOrigin(origin, { privateHosts });
  const db = store || bridgeStore({statePath,databaseUrl});
  const query = (sql,...parameters) => db.query(sql,parameters);
  const one = async (sql,...parameters) => (await query(sql,...parameters)).rows[0];
  const pending = new Map();
  async function encodeId(kind, original) {
    if (typeof original !== 'string' || !original || original.length > 1024) throw Error('authoritative ID required');
    const canonical = PREFIX[kind] + '_' + createHash('sha256').update(JSON.stringify([installationId, kind, original])).digest('hex');
    await query('INSERT INTO bridge_ids VALUES(?,?,?,?) ON CONFLICT DO NOTHING',installationId,kind,original,canonical);
    if ((await one('SELECT original FROM bridge_ids WHERE installation=? AND kind=? AND canonical=?',installationId,kind,canonical))?.original !== original) throw Error('ID collision');
    return canonical;
  }
  async function decodeId(kind, canonical) {
    const row = (await one('SELECT original FROM bridge_ids WHERE installation=? AND kind=? AND canonical=?',installationId,kind,canonical));
    if (!row) fail('RESOURCE_NOT_FOUND', 404);
    return row.original;
  }
  async function http(path, options = {}) {
    const response = await fetcher(new URL(path, base), { ...options, redirect: 'error', signal: AbortSignal.timeout(timeoutMs) });
    if (!response.ok) {
      let code = ''; try { const body = await response.json(); code = String(body.code || body.error?.code || ''); } catch { /* no success body */ }
      const known = ['INVALID_INPUT', 'UNAUTHENTICATED', 'FORBIDDEN', 'RESOURCE_NOT_FOUND', 'IDEMPOTENCY_CONFLICT', 'REVISION_CONFLICT', 'OUT_OF_STOCK'];
      const normalized = errorMap[code] || known.find(c => code === c) || (/INSUFFICIENT_STOCK|VARIANT_UNAVAILABLE/.test(code) ? 'OUT_OF_STOCK' :
        ({401:'UNAUTHENTICATED',403:'FORBIDDEN',404:'RESOURCE_NOT_FOUND'}[response.status] || 'UPSTREAM_ERROR'));
      fail(normalized, response.status);
    }
    if (!response.headers.get('content-type')?.includes('application/json')) throw Error('merchant response must be JSON');
    return response;
  }
  async function session(ctx) {
    const subject = ctx.subject;
    if (typeof subject !== 'string' || !subject) fail('INVALID_INPUT', 400, 'verified pairwise subject required');
    const stored = await one('SELECT cookie,expires FROM bridge_sessions WHERE installation=? AND subject=?',installationId,subject);
    if (stored) {
      if (stored.expires <= Date.now()) fail('UNAUTHENTICATED', 401, 'merchant buyer session expired; refusing silent replacement');
      return stored.cookie;
    }
    if (!pending.has(subject)) pending.set(subject, (async () => {
      const claim = await query('INSERT INTO bridge_session_claims VALUES(?,?) ON CONFLICT DO NOTHING',installationId,subject);
      if (claim.changes !== 1) {
        // A prior issuer call may have committed remotely. Never create a second
        // buyer identity after a crash, timeout, or an unconfirmed database save.
        for(let i=0;i<100;i++) {
          const existing=await one('SELECT cookie,expires FROM bridge_sessions WHERE installation=? AND subject=?',installationId,subject);
          if(existing) { if(existing.expires<=Date.now())fail('UNAUTHENTICATED',401); return existing.cookie; }
          await new Promise(resolve=>setTimeout(resolve,100));
        }
        fail('UPSTREAM_ERROR',503,'buyer session issuance is unconfirmed; reconciliation required');
      }
      const response = await http(sessionConfig.path, { method: 'POST', headers: {'content-type':'application/json'}, body: '{}' });
      const candidates = response.headers.getSetCookie().filter(c => c.startsWith(sessionConfig.cookie_name + '='));
      if (candidates.length !== 1) throw Error('session issuer must return one configured cookie');
      const cookie = candidates[0].split(';')[0];
      if (!/^[\w-]+=[^;\r\n]+$/.test(cookie)) throw Error('unsafe session cookie');
      const maxAge = /;\s*Max-Age=(\d+)(?:;|$)/i.exec(candidates[0]);
      const expires = /;\s*Expires=([^;]+)/i.exec(candidates[0]);
      const deadline = maxAge ? Date.now() + Number(maxAge[1]) * 1000 : expires ? Date.parse(expires[1]) : NaN;
      if (!Number.isFinite(deadline) || deadline <= Date.now()) throw Error('authoritative session expiry required');
      await query('INSERT INTO bridge_sessions VALUES(?,?,?,?)',installationId,subject,cookie,deadline);
      return cookie;
    })().finally(() => pending.delete(subject)));
    return pending.get(subject);
  }
  return { base, query, one, encodeId, decodeId, http, session, close: () => db.close() };
}
