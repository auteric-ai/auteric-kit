import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readFileSync,existsSync,mkdirSync,rmSync,realpathSync,symlinkSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {integrationDossier,installModule,disconnectModule} from '../src/connect/module.js';
import {shared} from '../src/connect/shared.js';

async function fixture() {
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-module-'));
  mkdirSync(join(root,'server'));writeFileSync(join(root,'server/app.js'),'export const boundary = 1;\n');
  writeFileSync(join(root,'package.json'),'{"type":"module"}');
  const {OPERATIONS,REGISTRY_DIGEST}=await shared('contracts.generated');
  const adapter=`export const schema='auteric-adapter/v1';
export const writes=['create_cart'];
export const auth={create_cart:'session'};
export const session={path:'/guest',cookie_name:'merchant_session'};
export const merchant=()=>({create_cart:()=>({})});
export const projections={create_cart:()=>({})};
`;
  const plan={files:{'auteric/adapter.mjs':adapter,'auteric/connection.json':JSON.stringify({schema:'auteric-module-connection/v1',
    registry_digest:REGISTRY_DIGEST,adapter:'./adapter.mjs',operations:['create_cart'],writes:['create_cart'],application:{module:'server/app.js'}}),
    'auteric/tests/fixtures.mjs':'export const fixture = true;\n'},
    hooks:[{path:'server/app.js',before:'export const boundary = 1;',after:'export const boundary = 1; // owned hook'}],
    capabilities:Object.fromEntries(Object.keys(OPERATIONS).map(op=>[op,{classification:op==='create_cart'?'internal_service':'implementation_required',
      reason:op==='create_cart'?'traced fixture service':'not proven',evidence:['server/app.js']}]))};
  return {root,plan,cleanup:()=>rmSync(root,{recursive:true,force:true})};
}

test('ownership begins during inventory; disconnect reverses hooks and preserves unrelated edits',async()=>{
  const f=await fixture();
  try {
    await integrationDossier(f.root);await installModule(f.root,f.plan);
    const app=join(f.root,'server/app.js');writeFileSync(app,readFileSync(app,'utf8')+'// unrelated merchant change\n');
    const result=await disconnectModule(f.root);
    assert.equal(result.hooks_reversed,1);assert.equal(existsSync(join(f.root,'auteric')),false);
    assert.equal(readFileSync(app,'utf8'),'export const boundary = 1;\n// unrelated merchant change\n');
    assert.equal(existsSync(join(f.root,'.auteric')),false);
  } finally {f.cleanup();}
});

test('edited artifact or hook refuses all cleanup before any merchant bytes change',async()=>{
  for(const edited of ['auteric/adapter.mjs','server/app.js']) {
    const f=await fixture();
    try {
      await installModule(f.root,f.plan);
      const file=join(f.root,edited),original=readFileSync(file,'utf8');
      writeFileSync(file,edited.startsWith('auteric/')?original+'// merchant edit\n':original.replace('owned hook','merchant hook'));
      const before=readFileSync(join(f.root,'server/app.js'),'utf8');
      await assert.rejects(disconnectModule(f.root),/disconnect_cleanup_required/);
      assert.equal(readFileSync(join(f.root,'server/app.js'),'utf8'),before);
      assert.equal(existsSync(join(f.root,'auteric/connection.json')),true);
    } finally {f.cleanup();}
  }
});

test('escape paths and missing capability proof are rejected before generation',async()=>{
  const f=await fixture();
  try {
    delete f.plan.capabilities.get_cart;
    await assert.rejects(installModule(f.root,f.plan),/classification missing/);
    assert.equal(existsSync(join(f.root,'auteric')),false);
    const {containedFile}=await shared('module-adapter');
    mkdirSync(join(f.root,'auteric'));symlinkSync(join(f.root,'server/app.js'),join(f.root,'auteric/escape.mjs'));
    assert.throws(()=>containedFile(join(f.root,'auteric'),'escape.mjs'),/escapes/);
  } finally {f.cleanup();}
});

test('an identical preexisting merchant artifact is preserved rather than adopted',async()=>{
  const f=await fixture();
  try {
    mkdirSync(join(f.root,'auteric'));
    const path=join(f.root,'auteric/adapter.mjs');writeFileSync(path,f.plan.files['auteric/adapter.mjs']);
    await installModule(f.root,f.plan);await disconnectModule(f.root);
    assert.equal(readFileSync(path,'utf8'),f.plan.files['auteric/adapter.mjs']);
    assert.equal(existsSync(join(f.root,'auteric/connection.json')),false);
  } finally {f.cleanup();}
});

test('owned operations require real session semantics; malformed write output stays uncertain',async()=>{
  const f=await fixture();
  try {
    await installModule(f.root,f.plan);
    const {loadAdapter,moduleAdapters}=await shared('module-adapter'),connection=join(f.root,'auteric/connection.json');
    const file=join(f.root,'auteric/adapter.mjs'),original=readFileSync(file,'utf8');
    writeFileSync(file,original.replace("auth={create_cart:'session'}",'auth={}'));
    await assert.rejects(loadAdapter(connection),/authoritative merchant session/);
    writeFileSync(file,original);
    await assert.rejects(moduleAdapters(connection,{bindingDigest:'sha256:'+'0'.repeat(64)}),/registered binding/);
    let mutations=0;
    const translator=await moduleAdapters(connection,{origin:'http://127.0.0.1:9901',installationId:'installation_test',
      applicationToken:'t'.repeat(32),statePath:join(f.root,'auteric/.state/bridge.sqlite'),fetcher:async(url,options)=>{
        if(new URL(url).pathname==='/guest')return new Response('{}',{headers:{'content-type':'application/json','set-cookie':'merchant_session=opaque; Max-Age=3600'}});
        assert.equal(options.headers.cookie,'merchant_session=opaque');mutations++;
        return new Response('{}',{headers:{'content-type':'application/json'}});
      }});
    try {
      const ctx={installationId:'installation_test',operation:'create_cart',subject:'guest_00000000',principal:'guest_00000000',
        actionId:'action_00000000',contractVersion:'1.0.0',pathParams:{}};
      await assert.rejects(translator.adapters.create_cart(ctx,{currency:'USD'}),e=>e.code==='EXECUTION_UNCERTAIN');
      assert.equal(mutations,1);
      await assert.rejects(translator.adapters.create_cart({...ctx,installationId:'other'},{currency:'USD'}),e=>e.code==='INVALID_INPUT');
      assert.equal(mutations,1);
    } finally {await translator.close();}
  } finally {f.cleanup();}
});


test('one model-led Connect request returns an internal continuation and auto-installs its plan',async()=>{
  const f=await fixture();
  const {run}=await import('../src/cli.js');
  try {
    const args=['connect','--domain','shop.example','--runtime-source',f.root,'--runtime-commit','deliberately-unavailable-baseline'];
    const handoff=await run(args,f.root);
    assert.equal(handoff.continuation.owner,'current_coding_model');
    assert.equal(handoff.continuation.stop_for_user,false);
    assert.deepEqual(handoff.continuation.resume_command,args);
    assert.ok(handoff.continuation.skill.endsWith('/auteric-connect/SKILL.md'));
    assert.match(handoff.next_action,/Do not ask the user/);
    writeFileSync(join(f.root,handoff.continuation.plan),JSON.stringify(f.plan));
    // Acceptance is automatic, without --adapter-plan or --local-acceptance.
    // This artificial merchant lacks Git/runtime prerequisites, so it cannot pass.
    await assert.rejects(run(args,f.root));
    assert.equal(existsSync(join(f.root,'auteric/connection.json')),true);
    assert.match(readFileSync(join(f.root,'server/app.js'),'utf8'),/owned hook/);
    assert.notEqual(JSON.parse(readFileSync(join(f.root,'auteric/.state/config.json'))).status,'local_verified');
    await disconnectModule(f.root);
    assert.equal(existsSync(join(f.root,'auteric')),false);
  } finally {f.cleanup();}
});

test('managed binding contains only registry subset/fingerprint; drift cannot match enrollment',async()=>{
  const f=await fixture();
  try {
    await installModule(f.root,f.plan);
    const {moduleBinding}=await shared('module-adapter'),{digest,validateMapping}=await shared('mapping');
    const binding=await moduleBinding(join(f.root,'auteric/connection.json'));
    assert.equal(validateMapping(binding),binding);
    assert.deepEqual(binding.operations,[{operation:'create_cart',side_effect:'write'}]);
    assert.equal(Object.hasOwn(binding,'adapter'),false);
    const before=digest(binding);
    writeFileSync(join(f.root,'auteric/adapter.mjs'),f.plan.files['auteric/adapter.mjs']+'// changed adapter\n');
    assert.notEqual(digest(await moduleBinding(join(f.root,'auteric/connection.json'))),before);
    assert.throws(()=>validateMapping({...binding,operations:[{operation:'create_cart',side_effect:'read'}]}),/effects/);
  } finally {f.cleanup();}
});

test('discovery hook forwards exact manager bytes and fails closed without replacing merchant business routes',async()=>{
  const {mountPrivate}=await import('../src/connect/private-hook.mjs');
  const routes=new Map(),oldOrigin=process.env.AUTERIC_RUNTIME_ORIGIN,oldFetch=globalThis.fetch;
  const app={get:(path,callback)=>routes.set(path,callback),use:()=>{}};
  const response=()=>({statusCode:200,headers:{},status(code){this.statusCode=code;return this;},
    json(body){this.body=body;return this;},set(k,v){this.headers[k]=v;return this;},type(v){this.headers.type=v;return this;},send(body){this.body=body;return this;}});
  try {
    process.env.AUTERIC_RUNTIME_ORIGIN='http://127.0.0.1:7080';
    mountPrivate({app,registerRoute:()=>{},services:{}},{merchant:()=>({}),writes:[]},{operations:[],writes:[]},'t'.repeat(32));
    const exact='{ "ucp": {"version":"issued-by-control"} }\n';
    globalThis.fetch=async(url)=>{assert.equal(String(url),'http://127.0.0.1:7080/.well-known/ucp');return new Response(exact,{headers:{'content-type':'application/json'}});};
    const res=response();await routes.get('/.well-known/ucp')({},res);
    assert.equal(res.body,exact);assert.equal(res.headers['Cache-Control'],'no-store');
    globalThis.fetch=async()=>new Response('{}',{status:503});
    const closed=response();await routes.get('/.well-known/ucp')({},closed);assert.equal(closed.statusCode,503);
  } finally {globalThis.fetch=oldFetch;if(oldOrigin===undefined)delete process.env.AUTERIC_RUNTIME_ORIGIN;else process.env.AUTERIC_RUNTIME_ORIGIN=oldOrigin;}
});
