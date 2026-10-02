import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, existsSync, mkdirSync, rmSync, realpathSync, symlinkSync, readdirSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { disconnectHTTP } from '../src/connect/disconnect.js';
import { writeArtifacts, removeArtifacts, initializeManaged } from '../src/connect/artifacts.js';
import { projectDigest } from '../src/workflow.js';

async function fixture() {
  const root=mkdtempSync(join(realpathSync(tmpdir()),'auteric-disconnect-'));
  writeFileSync(join(root,'merchant.json'),'{"unchanged":true}');
  await writeArtifacts(root,{'auteric/connection.json':'{"owned":true}\n','auteric/compose.yaml':'name: owned-runtime\n'});
  writeFileSync(join(root,'auteric/.state/bridge-test.sqlite'),'durable state');
  mkdirSync(join(root,'auteric/.state/identity'),{mode:0o700});
  writeFileSync(join(root,'auteric/.state/identity/enrollment.json'),JSON.stringify({installation_id:'install_a',token:'private-scoped-credential'}),{mode:0o600});
  const discovery='{"ucp":{"version":"synthetic-only"}}\n';
  mkdirSync(join(root,'auteric/discovery'));
  writeFileSync(join(root,'auteric/discovery/ucp'),discovery);
  writeFileSync(join(root,'auteric/discovery/.auteric-owner.json'),JSON.stringify({installation_id:'install_a',digest:'sha256:'+createHash('sha256').update(discovery).digest('hex')}));
  const state={runtime_enrollment:true,store_id:'store_a',installation_id:'install_a',deployment_platform:'compose',api_url:'https://control.example',domain:'merchant.example',status:'deployment_pending'};
  const events=[];
  const callbacks={authenticate:async()=>{events.push('owner');return {access_token:'synthetic-owner'};},
    request:async(base,path,options)=>{events.push(options.method+' '+path);return {};},
    stopCompose:async path=>{events.push('stop');assert.equal(path,join(root,'auteric/compose.yaml'));assert.ok(existsSync(path));}};
  return {root,state,events,callbacks,close:()=>rmSync(root,{recursive:true,force:true})};
}
test('disconnect revokes only its installation, stops owned service, removes generated artifacts and preserves audit/state',async()=>{
  const f=await fixture();
  try {
    const result=await disconnectHTTP(f.root,f.state,f.callbacks);
    assert.deepEqual(f.events,['owner','POST /api/commerce/stores/store_a/installations/install_a/revoke','DELETE /api/commerce/stores/store_a/runtime-enrollment/install_a','stop']);
    assert.equal(result.status,'disconnected');
    for(const path of ['connection.json','compose.yaml','discovery/ucp','.state/identity/enrollment.json'])assert.equal(existsSync(join(f.root,'auteric',path)),false);
    assert.equal(readFileSync(join(f.root,'auteric/.state/bridge-test.sqlite'),'utf8'),'durable state');
    assert.deepEqual(readdirSync(f.root).sort(),['auteric','merchant.json']);
    assert.equal(readFileSync(join(f.root,'merchant.json'),'utf8'),'{"unchanged":true}');
    await disconnectHTTP(f.root,result,{authenticate:()=>{throw Error('no-op must not authenticate');}});
  } finally {f.close();}
});
test('revocation failure preserves credentials and artifacts without stopping runtime',async()=>{
  const f=await fixture();
  try {
    await assert.rejects(disconnectHTTP(f.root,f.state,{...f.callbacks,request:async()=>{throw Error('control offline');}}),/offline/);
    assert.equal(f.events.includes('stop'),false);
    assert.equal(existsSync(join(f.root,'auteric/connection.json')),true);
    assert.equal(existsSync(join(f.root,'auteric/.state/identity/enrollment.json')),true);
    const status=JSON.parse(readFileSync(join(f.root,'auteric/.state/connection-status.json')));
    assert.equal(status.status,'revocation_pending');assert.equal(status.production_ready,false);
  } finally {f.close();}
});
test('interrupted local stop is resumable after revocation without another owner session',async()=>{
  const f=await fixture();
  try {
    await assert.rejects(disconnectHTTP(f.root,f.state,{...f.callbacks,stopCompose:async()=>{throw Error('Docker unavailable');}}),/unavailable/);
    const saved=JSON.parse(readFileSync(join(f.root,'auteric/.state/config.json')));
    assert.equal(saved.remote_revoked,true);assert.equal(saved.local_cleanup_pending,true);
    const resumed=await disconnectHTTP(f.root,saved,{...f.callbacks,authenticate:()=>{throw Error('already revoked');}});
    assert.equal(resumed.status,'disconnected');
    assert.equal(f.events.filter(event=>event==='owner').length,1);
  } finally {f.close();}
});
test('edited Compose cannot be executed automatically; edited artifacts/discovery survive cleanup',async()=>{
  const f=await fixture();
  try {
    writeFileSync(join(f.root,'auteric/compose.yaml'),'merchant modification');
    await assert.rejects(disconnectHTTP(f.root,f.state,f.callbacks),/missing or edited/);
    assert.equal(f.events.includes('stop'),false);
    writeFileSync(join(f.root,'auteric/discovery/ucp'),'merchant discovery modification');
    const result=await removeArtifacts(f.root,'install_a');
    assert.deepEqual(result.retained.sort(),['auteric/compose.yaml','auteric/discovery/ucp']);
  } finally {f.close();}
});
test('symlinks, missing private ignores and world-readable private directories fail closed',async()=>{
  const f=await fixture();
  try {
    rmSync(join(f.root,'auteric/connection.json'));symlinkSync(join(f.root,'merchant.json'),join(f.root,'auteric/connection.json'));
    await assert.rejects(removeArtifacts(f.root,'install_a'),/symlink/);
    writeFileSync(join(f.root,'auteric/.gitignore'),'# merchant edits\n');
    assert.throws(()=>initializeManaged(f.root),/gitignore/);
    writeFileSync(join(f.root,'auteric/.gitignore'),'/.state/\n/discovery/\n');
    chmodSync(join(f.root,'auteric/.state'),0o755);
    assert.throws(()=>initializeManaged(f.root),/private/);
  } finally {f.close();}
});
test('generated Auteric state does not change merchant resume digest',async()=>{
  const f=await fixture();
  try {
    const before=projectDigest(f.root);
    writeFileSync(join(f.root,'auteric/.state/health.json'),'{"health":"new"}');
    assert.equal(projectDigest(f.root),before);
    writeFileSync(join(f.root,'merchant.json'),'{"changed":true}');
    assert.notEqual(projectDigest(f.root),before);
  } finally {f.close();}
});
