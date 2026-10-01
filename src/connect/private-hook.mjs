// Small application-side transport template. No SDK or merchant business implementation.
import { timingSafeEqual } from 'node:crypto';
export const integration = adapter => process.env.AUTERIC_APPLICATION_TOKEN
  ? boundary => mountPrivate(boundary, adapter, {operations:Object.keys(adapter.projections),writes:adapter.writes}, process.env.AUTERIC_APPLICATION_TOKEN)
  : null;
export function mountPrivate({ app, registerRoute, services }, adapter, config, token) {
  if (typeof token !== 'string' || token.length < 32) throw Error('private application token required');
  const runtimeOrigin=process.env.AUTERIC_RUNTIME_ORIGIN;
  if(runtimeOrigin) {
    const url=new URL(runtimeOrigin);
    if(!['http:','https:'].includes(url.protocol) || url.username || url.password || url.pathname!=='/' || url.search || url.hash)
      throw Error('runtime origin must be a configured private origin');
    // Serve exact, current manager-issued discovery; never snapshot test endpoints.
    app.get('/.well-known/ucp',async (_req,res)=>{
      try {
        const upstream=await fetch(new URL('/.well-known/ucp',url),{redirect:'error',signal:AbortSignal.timeout(5000)});
        if(!upstream.ok || !upstream.headers.get('content-type')?.includes('application/json'))return res.status(503).json({code:'AUTERIC_NOT_READY'});
        const text=await upstream.text();
        if(Buffer.byteLength(text)>65536 || !JSON.parse(text)?.ucp)return res.status(503).json({code:'AUTERIC_NOT_READY'});
        res.set('Cache-Control','no-store');res.type('application/json').send(text);
      } catch {res.status(503).json({code:'AUTERIC_NOT_READY'});}
    });
  }
  const handlers = adapter.merchant(services);
  app.use('/api/auteric/private', (req, res, next) => {
    const a = Buffer.from(String(req.headers.authorization || '')), b = Buffer.from('Bearer ' + token);
    if (a.length !== b.length || !timingSafeEqual(a, b)) return res.status(401).json({code:'SESSION_REQUIRED'});
    next();
  });
  for (const operation of config.operations) {
    const write = config.writes.includes(operation);
    if (typeof handlers[operation] !== 'function') throw Error('missing merchant handler: ' + operation);
    registerRoute(write ? 'post' : 'get', '/api/auteric/private/' + operation,
      {auth:adapter.auth?.[operation] || 'none', idempotent:write}, ctx => {
        let input;
        try { input = write ? ctx.req.body : JSON.parse(ctx.req.query.input || '{}'); }
        catch { throw Object.assign(new Error('Invalid input'), {status:400,code:'INVALID_INPUT'}); }
        if (!input || typeof input !== 'object' || Array.isArray(input))
          throw Object.assign(new Error('Invalid input'), {status:400,code:'INVALID_INPUT'});
        const result = handlers[operation](ctx, input);
        if (result?.then) throw Error('transaction callback must be synchronous');
        return result;
      });
  }
}
