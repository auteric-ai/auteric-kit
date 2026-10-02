import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, existsSync, symlinkSync, mkdirSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { shared } from '../src/connect/shared.js';
import { mappingFromOpenAPI } from '../src/connect/mapping.js';
import { writeArtifacts } from '../src/connect/artifacts.js';
import { renderCompose } from '../src/deploy/compose.js';
import { renderECS } from '../src/deploy/ecs.js';
import { prepareHTTP } from '../src/connect/prepare.js';
import { credentialTarget, deliverCredential } from '../src/connect/credentials.js';
import { run } from '../src/cli.js';
import { startMerchant, fixtureMapping } from '../runtime/merchant-runtime/tests/merchant-fixture.js';
import { testBridge } from '../src/connect/test-bridge.js';

function specFor(mapping) {
  const {operations,...metadata}=mapping;delete metadata.schema;
  const paths={[mapping.session.path]:{post:{operationId:'issue_guest'}}};
  for(const op of operations){const {operation,target,...config}=op;paths[target.path]||={};paths[target.path][target.method.toLowerCase()]={operationId:operation,'x-auteric':config};}
  return {openapi:'3.0.3',info:{title:'Merchant',version:'1'},'x-auteric':metadata,paths};
}
const deployment={schema:'auteric-deployment/v1',platform:'compose',runtime_image_digest:'registry.example/runtime@sha256:'+'a'.repeat(64),architecture:'linux/amd64',secret_ref:'file:/external/identity/secret.json',state_ref:{profile:'gateway/v1',reference:'merchant_state'},discovery_mount:'/srv/discovery',network_ref:'merchant_private',merchant_service:'shop',runtime_port:7080,runtime_user:'10001:10001'};
async function qualifiedTestRelease(){const {bundledRelease}=await shared('release');return {...bundledRelease(),image:deployment.runtime_image_digest,architectures:['linux/amd64'],deployment_profiles:['compose:gateway/v1'],qualification:{passed:true,anonymous_pull:true,clean_install:true}};}
async function connection(){const {digest}=await shared('mapping');const mapping=fixtureMapping();return {schema:'auteric-connection/v1',domain:'shop.example',environment:'staging',control_origin:'https://control.example',store_id:'store_test',installation_id:'installation_a',runtime_release:'test',backend:{transport:'http',origin:'http://shop:3000'},mapping_digest:digest(mapping),mapping};}

test('OpenAPI selection uses explicit provenance, never route-name guesses',async()=>{
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-spec-'));
  try{const path=join(root,'openapi.json'),mapping=fixtureMapping('b');writeFileSync(path,JSON.stringify(specFor(mapping)));
    assert.deepEqual(await mappingFromOpenAPI(path),mapping);
    const spec=specFor(mapping);delete spec.paths[mapping.session.path];writeFileSync(path,JSON.stringify(spec));await assert.rejects(mappingFromOpenAPI(path),/provenance/);
  }finally{rmSync(root,{recursive:true,force:true});}
});
test('renderers preserve infrastructure and keep Bridge private; unqualified storage fails',async()=>{
  const release=await qualifiedTestRelease(),doc=await connection();
    const compose=await renderCompose(doc,deployment,release);assert.doesNotMatch(compose,/7071|ports:|build:/);assert.match(compose,/external: true/);assert.match(compose,/healthcheck:/);
    assert.match(compose,/\.\/connection.json:/);assert.doesNotMatch(compose,/\.\/auteric\/connection.json:/);
    const other=await renderCompose({...doc,installation_id:'another-installation'},deployment,release);
    assert.notEqual(compose.split('\n')[0],other.split('\n')[0],'different installations must not share a Compose project');
  const {bundledRelease}=await shared('release');await assert.rejects(renderCompose(doc,deployment,{...bundledRelease(),qualification:{passed:false}}),/release_unqualified/);
  await assert.rejects(renderECS({networkMode:'awsvpc'}, {...doc,backend:{transport:'http',origin:'http://127.0.0.1:3000'}},{...deployment,platform:'ecs-fargate',secret_ref:'arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime'},release),/storage_profile|schema/);
});
test('merchant artifacts are no-op on rerun and preserve edits and symlink targets',async()=>{
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-artifacts-'));
  try{const files={'auteric/connection.json':'{}\n','auteric/compose.yaml':'name: owned-runtime\n'};
    assert.equal((await writeArtifacts(root,files,{dryRun:true})).files_written,0);assert.equal(existsSync(join(root,'auteric')),false);
    assert.equal((await writeArtifacts(root,files)).files_written,2);assert.equal((await writeArtifacts(root,files)).no_op,true);
    writeFileSync(join(root,'auteric/compose.yaml'),'merchant edit');await assert.rejects(writeArtifacts(root,files),/artifact_conflict/);assert.equal(readFileSync(join(root,'auteric/compose.yaml'),'utf8'),'merchant edit');
    await assert.rejects(writeArtifacts(root,{'public/.well-known/ucp':'{}'}),/unknown/);
    const other=join(root,'outside');mkdirSync(other);symlinkSync(other,join(root,'redirect'));
    const {atomicWrite}=await shared('atomic');assert.throws(()=>atomicWrite(join(root,'redirect/config'),'unsafe'),/symlink/);
  }finally{rmSync(root,{recursive:true,force:true});}
});
test('CLI default creates no vendor, lifecycle or coding-agent files; dry-run changes nothing',async()=>{
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-prepare-'));
  try{
    writeFileSync(join(root,'package.json'),'{"type":"module","dependencies":{"express":"4"}}');
    const state=await run(['connect','--domain','shop.example','--backend','.','--frontend','.','--no-agent'],root);
    assert.equal(state.integration,'implementation_required');assert.equal(state.production_ready,false);assert.deepEqual(state.tested_operations,[]);
    assert.equal(existsSync(join(root,'server')),false);assert.equal(existsSync(join(root,'auteric/vendor')),false);assert.equal(existsSync(join(root,'.auteric/connector.json')),false);
    assert.equal(existsSync(join(root,'.auteric')),false);
    const before=readFileSync(join(root,'auteric/.state/connection-status.json'),'utf8');await run(['connect','--domain','shop.example','--dry-run'],root);assert.equal(readFileSync(join(root,'auteric/.state/connection-status.json'),'utf8'),before);
    await run(['disconnect'],root);assert.equal(existsSync(join(root,'.auteric')),false);
  }finally{rmSync(root,{recursive:true,force:true});}
});
test('release qualification gates registration before owner authentication',async()=>{
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-release-gate-'));
  try{writeFileSync(join(root,'openapi.json'),JSON.stringify(specFor(fixtureMapping())));writeFileSync(join(root,'deploy.json'),JSON.stringify(deployment));
    await assert.rejects(prepareHTTP(root,{mapping:'openapi.json',deployment:'deploy.json',environment:'staging'},{layout:{backend:root},base:'https://control.example',domain:'shop.example',authenticate:()=>{throw Error('authentication should not run');},request:()=>{throw Error('Control should not be mutated');}}),/release_unqualified|unknown runtime image/);
    assert.equal(existsSync(join(root,'auteric/connection.json')),false);assert.equal(existsSync(join(root,'.auteric')),false);
  }finally{rmSync(root,{recursive:true,force:true});}
});
test('mapping input in the root auteric folder resolves independently of a nested backend',async()=>{
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-nested-backend-'));
  try {
    const backend=join(root,'server');mkdirSync(backend);mkdirSync(join(root,'auteric'));
    writeFileSync(join(backend,'package.json'),'{"dependencies":{"express":"4"}}');
    writeFileSync(join(root,'auteric/openapi.json'),JSON.stringify(specFor(fixtureMapping())));
    const result=await prepareHTTP(root,{mapping:'auteric/openapi.json'},{
      layout:{backend},base:'https://control.example',domain:'shop.example',dryRun:true,
      authenticate:()=>{throw Error('preview must not authenticate');}});
    assert.equal(result.status,'prepared_preview');assert.equal(result.operations.length,5);
    assert.equal(existsSync(join(root,'auteric/.state')),false);
    assert.equal(existsSync(join(backend,'auteric')),false);
    assert.equal(existsSync(join(root,'.auteric')),false);
  }finally{rmSync(root,{recursive:true,force:true});}
});
test('enrollment delivery respects non-root secret ownership and preserves scoped identity',async()=>{
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-secrets-')),merchant=join(root,'merchant'),path=join(merchant,'auteric/.state/identity/enrollment.json');
  mkdirSync(merchant);
  try {
    const target={...deployment,secret_ref:'file:'+path,runtime_user:`${process.getuid()}:${process.getgid()}`};
    assert.equal(await credentialTarget(merchant,target),path);
    await deliverCredential(path,{installation_id:'installation-test',token:'test-service-identity'.repeat(3),expires_at:12345});
    const original=readFileSync(path,'utf8');
    await deliverCredential(path,{installation_id:'installation-test',no_op:true});assert.equal(readFileSync(path,'utf8'),original);
    await assert.rejects(deliverCredential(path,{installation_id:'other-installation',token:'other'}),/different installation/);
    await assert.rejects(credentialTarget(merchant,{...target,runtime_user:`${process.getuid()+1}:${process.getgid()}`}),/runtime UID/);
    await assert.rejects(credentialTarget(root,target),/auteric\/\.state/);
  }finally{rmSync(root,{recursive:true,force:true});}
});
for(const style of ['a','b'])test(`shared HTTP harness on independent ${style} merchant`,async()=>{
  const dir=mkdtempSync(join(realpathSync(tmpdir()),'auteric-harness-'));let merchant;
  try{merchant=await startMerchant(join(dir,'merchant.sqlite'),style);
    const result=await testBridge(merchant.mapping,{origin:merchant.origin,installationId:'isolated-test',statePath:join(dir,'bridge.sqlite'),query:'shoe',currency:'USD'});
    assert.equal(result.status,'passed',result.reason);assert.equal(result.operations.length,5);assert.equal(result.enforcement_verified,false);
  }finally{await merchant?.close();rmSync(dir,{recursive:true,force:true});}
});

test('module ECS renderer delivers adapter separately and preserves merchant command/identity boundary',async()=>{
  const {digest}=await shared('mapping');
  const mapping={schema:'auteric-module-binding/v1',registry_digest:(await shared('contracts.generated')).REGISTRY_DIGEST,
    adapter_fingerprint:'sha256:'+'b'.repeat(64),operations:[{operation:'get_product',side_effect:'read'}]};
  const doc={...(await connection()),backend:{transport:'http',origin:'http://127.0.0.1:8080'},mapping,mapping_digest:digest(mapping)};
  const config={...deployment,platform:'ecs-fargate',secret_ref:'arn:aws:secretsmanager:us-east-2:123456789012:secret:runtime',
    application_secret_ref:'arn:aws:secretsmanager:us-east-2:123456789012:secret:private-application',
    state_ref:{profile:'gateway/v1',reference:'arn:aws:secretsmanager:us-east-2:123456789012:secret:runtime-database'},
    discovery_mount:'discovery',adapter_mount:'integration'};
  const release={...(await qualifiedTestRelease()),deployment_profiles:['ecs-fargate:gateway/v1']};
  const task={family:'merchant',networkMode:'awsvpc',requiresCompatibilities:['FARGATE'],executionRoleArn:'role',
    volumes:[{name:'discovery'}],containerDefinitions:[{name:'shop',image:'merchant-image',command:['node','server/index.js'],
      healthCheck:{command:['CMD','health']},environment:[{name:'PORT',value:'8080'}]}]};
  const {taskDefinition:out,ingress}=await renderECS(task,doc,config,release);
  assert.equal(task.containerDefinitions.length,1,'input task remains unchanged');
  assert.equal(out.containerDefinitions.length,2);
  assert.deepEqual(out.containerDefinitions[0].command,['node','server/index.js']);
  assert.ok(out.containerDefinitions[0].environment.some(e=>e.name==='AUTERIC_RUNTIME_ORIGIN'));
  const runtime=out.containerDefinitions[1];
  assert.equal(runtime.image,deployment.runtime_image_digest);
  assert.ok(runtime.environment.some(e=>e.name==='AUTERIC_ADAPTER_CONNECTION'&&e.value==='/integration/connection.json'));
  assert.ok(runtime.mountPoints.some(m=>m.containerPath==='/integration'&&m.readOnly));
  assert.deepEqual(runtime.portMappings,[{containerPort:7080,protocol:'tcp'}]);
  assert.ok(ingress.paths.includes('/.well-known/ucp'));
  await assert.rejects(renderECS({...task,containerDefinitions:[{...task.containerDefinitions[0],healthCheck:undefined}]},doc,config,release),/merchant healthcheck/);
});
