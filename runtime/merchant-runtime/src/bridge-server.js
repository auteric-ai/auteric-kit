import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { validateError } from './contracts.generated.js';

const ERRORS = Object.freeze({
  INVALID_INPUT:[400,false], UNAUTHENTICATED:[401,false], FORBIDDEN:[403,false],
  RESOURCE_NOT_FOUND:[404,false], REVISION_CONFLICT:[409,true], OUT_OF_STOCK:[409,false],
  IDEMPOTENCY_CONFLICT:[409,false], CAPABILITY_DISABLED:[403,false], CONTRACT_MISMATCH:[421,false],
  RATE_LIMITED:[429,true], EXECUTION_UNCERTAIN:[504,false], UPSTREAM_ERROR:[502,true], SSRF_BLOCKED:[502,false],
});
function failure(error, context) {
  const code=Object.hasOwn(ERRORS,error.code)?error.code:'UPSTREAM_ERROR';
  const [status,retryable]=ERRORS[code];
  const action_id=/^action_[A-Za-z0-9]{8,64}$/.test(context?.actionId||'')?context.actionId:'action_00000000';
  const value={error:{code,message:'Merchant operation rejected: '+code,retryable,action_id}};
  validateError(value);
  return {status,value};
}

/** Private transport for the shared HTTP translator; it performs no MEP authorization. */
export function createApplicationBridge({ adapters, token, host = '127.0.0.1', port = 0 }) {
  if (typeof token !== 'string' || token.length < 32) throw Error('bridge token requires 32 characters');
  if (!['127.0.0.1','::1','localhost'].includes(host)) throw Error('combined runtime bridge is loopback-only');
  const reply = (res, status, value) => { if (!res.headersSent) res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); };
  const server = createServer((req, res) => {
    void (async () => {
      const supplied = Buffer.from(String(req.headers.authorization || '')), expected = Buffer.from('Bearer ' + token);
      if (supplied.length !== expected.length || !timingSafeEqual(supplied,expected)) return reply(res,401,{error:{code:'UNAUTHENTICATED'}});
      if (req.method === 'GET' && req.url === '/health/ready') return reply(res,200,{status:'ready',operations:Object.keys(adapters).sort()});
      const match = req.method === 'POST' && /^\/invoke\/([a-z][a-z0-9_]*)$/.exec(req.url || '');
      if (!match || !Object.hasOwn(adapters,match[1])) return reply(res,404,{error:{code:'RESOURCE_NOT_FOUND'}});
      let size = 0; const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1024 * 1024) {reply(res,413,{error:{code:'INVALID_INPUT'}}); req.resume(); return;}
        chunks.push(chunk);
      }
      let envelope;
      try {envelope=JSON.parse(Buffer.concat(chunks).toString('utf8'));} catch {return reply(res,400,{error:{code:'INVALID_INPUT'}});}
      if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope) || Object.keys(envelope).some(k=>!['context','input'].includes(k)) || !envelope.context || envelope.context.operation !== match[1]) return reply(res,400,{error:{code:'INVALID_INPUT'}});
      try { reply(res,200,await adapters[match[1]](Object.freeze({...envelope.context}),envelope.input)); }
      catch (error) {
        const result=failure(error.name==='ContractValidationError'?{code:'INVALID_INPUT'}:error,envelope.context);
        reply(res,result.status,result.value);
      }
    })().catch(() => reply(res,500,{error:{code:'UPSTREAM_ERROR'}}));
  });
  server.requestTimeout=15000;
  server.headersTimeout=10000;
  return {
    server,
    listen: () => new Promise((resolve,reject) => {
      server.once('error',reject);
      server.listen(port,host,()=>{server.off('error',reject);resolve({host,port:server.address().port});});
    }),
    close: () => new Promise((resolve,reject)=>server.close(e=>e?reject(e):resolve())),
  };
}
