import test from 'node:test';
import assert from 'node:assert/strict';
import {checkControl} from '../scripts/check-control.mjs';

const release={registry_digest:'sha256:registry',control_api:'auteric-runtime-enrollment/v1'};
const compatible={protocol:release.control_api,available:true,registry_digest:release.registry_digest,
 runtime_state_protocol:'auteric-runtime-state/v1',binding_kinds:['auteric-module-binding/v1'],
 environments:['production'],operations:['search_products']};
const reply=(status,body={})=>new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}});
function backend(overrides={}) {
 const calls=[];
 return {calls,fetchImpl:async(url,options)=>{
  const path=new URL(url).pathname;calls.push({path,method:options.method || 'GET'});
  if(overrides[path])return overrides[path](options);
  if(path==='/ready')return reply(200,{status:'ready'});
  if(path==='/api/commerce/runtime-compatibility')return reply(200,compatible);
  if(path==='/api/commerce/cli/start')return reply(200,{approval_mode:'browser'});
  return reply(401);
 },pause:async()=>{},log:()=>{},attempts:3};
}
test('live check tolerates transient deployment errors on read-only probes',async()=>{
 let calls=0;
 const api=backend({'/ready':()=>++calls<3?reply(503):reply(200)});
 assert.equal((await checkControl(release,api)).status,'passed');
 assert.equal(calls,3);
 assert.equal(api.calls.filter(c=>c.method==='POST').length,2);
});
test('live check still fails after its bounded readiness retries',async()=>{
 const api=backend({'/ready':()=>reply(503)});
 await assert.rejects(checkControl(release,api),/Control \/ready: 503/);
 assert.equal(api.calls.length,3);
});
test('missing runtime API is not retried or treated as compatible',async()=>{
 const api=backend({'/api/commerce/runtime-compatibility':()=>reply(404)});
 await assert.rejects(checkControl(release,api),/runtime-compatibility: 404/);
 assert.equal(api.calls.filter(c=>c.path.endsWith('runtime-compatibility')).length,1);
});
test('live check rejects incompatible or unavailable runtime without creating authorization requests',async()=>{
 for(const value of [{...compatible,available:false},{...compatible,registry_digest:'other'},{}]) {
  const api=backend({'/api/commerce/runtime-compatibility':()=>reply(200,value)});
  await assert.rejects(checkControl(release,api),/incompatible/);
  assert.equal(api.calls.filter(c=>c.method==='POST').length,0);
 }
});
test('authorization POST is never retried after an ambiguous failure',async()=>{
 const api=backend({'/api/commerce/cli/start':()=>reply(503)});
 await assert.rejects(checkControl(release,api),/cli\/start: 503/);
 assert.equal(api.calls.filter(c=>c.path.endsWith('cli/start')).length,1);
});
test('network errors retry only read-only probes and never weaken owner checks',async()=>{
 let calls=0;
 const api=backend({'/ready':()=>{if(++calls===1)throw new TypeError('network');return reply(200);},
  '/api/commerce/stores/not-owned/installations':()=>reply(200)});
 await assert.rejects(checkControl(release,api),/Owner authorization is not enforced/);
 assert.equal(calls,2);
});
