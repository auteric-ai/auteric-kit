import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { connectPSP, validatePSPPlan, resolvePSPAccount } from '../src/connect/psp.js';

const plan = () => ({schema:'auteric-psp-plan/v1',name:'Shared PSP',backend_origin:'https://psp.example',
  environment:'sandbox',platform_resource:'platform-a',stores:[
    {store_id:'store-a',merchant_id:'account-a'},{store_id:'store-b',merchant_id:'account-b'}]});
const rootWithPlan = value => {
  const root=mkdtempSync(join(realpathSync(tmpdir()),'psp-connect-'));
  mkdirSync(join(root,'auteric/.state'),{recursive:true,mode:0o700});
  writeFileSync(join(root,'auteric/.state/psp-plan.json'),JSON.stringify(value));
  return root;
};
const binding = (store,account) => ({psp_id:'platform-a',store_id:store,merchant_id:account,
  backend_origin:'https://psp.example',environment:'sandbox',binding_digest:'sha256:'+'a'.repeat(64),runtime_verified:false});
function harness(rejectSecond=false) {
  const calls=[];
  return {calls,base:'https://control.example',authorize:async()=>({authorized:true}),token:async()=> 'test-human-session',
    request:async(base,path,options={})=> {
      calls.push({path,...options});
      if(path.endsWith('/psp-compatibility'))return {protocol:'auteric-psp/v1',available:true};
      if(path.endsWith('/auth/me'))return {org:'org-a'};
      if(path.endsWith('/store-a/psp'))return {binding:binding('store-a','account-a')};
      if(path.endsWith('/store-b/psp')) {
        if(rejectSecond)throw Error('403 Store outside scope');
        return {binding:binding('store-b','account-b')};
      }
      return {};
    }};
}

test('connect psp is a real CLI mode and dry run needs no hosted service',()=> {
  const root=rootWithPlan(plan());
  // Run in the merchant task rather than in the Kit repository.
  const merchant=spawnSync(process.execPath,[new URL('../bin/auteric.js',import.meta.url).pathname,
    'connect','psp','--dry-run'],{cwd:root,encoding:'utf8'});
  assert.equal(merchant.status,2,merchant.stderr);
  assert.equal(JSON.parse(merchant.stdout).status,'prepared_preview');
});

test('missing plan is a current-model continuation, not successful connection',async()=> {
  const root=mkdtempSync(join(realpathSync(tmpdir()),'psp-dossier-'));
  const result=await connectPSP(root,{}, {inventory:async()=>({candidates:[]})});
  assert.equal(result.status,'implementation_required');
  assert.equal(result.production_ready,false);
  assert.equal(result.continuation.owner,'current_coding_model');
  assert.equal(result.continuation.stop_for_user,false);
});

test('duplicate accounts, credentials, domain drift and environment drift are rejected before writes',()=> {
  const invalid=[{...plan(),token:'secret'}, {...plan(),backend_origin:'https://user:secret@psp.example'},
    {...plan(),stores:[{store_id:'store-a',merchant_id:'account-a'},{store_id:'store-b',merchant_id:'account-a'}]}];
  for(const value of invalid)assert.throws(()=>validatePSPPlan(value));
  assert.throws(()=>validatePSPPlan(plan(),{domain:'other.example'}));
  assert.throws(()=>validatePSPPlan(plan(),{environment:'production'}));
});

test('owner authorization is resumable and cannot register memberships before approval',async()=> {
  const h=harness(); h.authorize=async()=>({authorized:false,status:'authentication_pending'});
  const result=await connectPSP(rootWithPlan(plan()),{},h);
  assert.equal(result.status,'authentication_pending');
  assert.equal(h.calls.filter(c=>c.method==='PUT'||c.method==='POST').length,0);
});

test('one backend produces two scoped bindings, without grants or live claims',async()=> {
  const h=harness(),root=rootWithPlan(plan());
  const result=await connectPSP(root,{},h);
  assert.equal(result.status,'membership_registered');
  assert.equal(result.production_ready,false);
  const manifest=JSON.parse(readFileSync(join(root,'auteric/psp.json')));
  assert.equal(resolvePSPAccount(manifest,{storeId:'store-a',merchant_id:'account-b'}).merchant_id,'account-a');
  assert.equal(resolvePSPAccount(manifest,{storeId:'store-b'}).merchant_id,'account-b');
  assert.throws(()=>resolvePSPAccount(manifest,{storeId:'other-store'}));
  assert.throws(()=>resolvePSPAccount(manifest,{merchant_id:'account-a'}));
  assert.ok(h.calls.every(c=>!c.path.includes('/grants')&&!c.path.includes('/policy')));
  const again=await connectPSP(root,{},h);
  assert.equal(again.psp_id,result.psp_id);
});

test('denied Store binding propagates and cannot produce a success manifest',async()=> {
  await assert.rejects(connectPSP(rootWithPlan(plan()),{},harness(true)),/403/);
});

test('new PSP and Stores register once and reuse saved identities on resume',async()=> {
  const value={...plan(),platform_resource:undefined,stores:[{name:'Shop',domain:'shop.example',merchant_id:'account-a'}]};
  const root=rootWithPlan(value),h=harness();
  const original=h.request;
  h.request=async(base,path,options)=> {
    if(path==='/api/commerce/control/resources') {h.calls.push({path,...options});return {id:'platform-a'};}
    if(path==='/api/commerce/stores') {h.calls.push({path,...options});return {id:'store-a'};}
    return original(base,path,options);
  };
  assert.equal((await connectPSP(root,{},h)).status,'membership_registered');
  assert.equal((await connectPSP(root,{},h)).status,'membership_registered');
  assert.equal(h.calls.filter(c=>c.path==='/api/commerce/stores').length,1);
  assert.equal(h.calls.filter(c=>c.path==='/api/commerce/control/resources').length,1);
});
