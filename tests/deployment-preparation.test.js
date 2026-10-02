import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,existsSync,rmSync,realpathSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {prepareDeployment} from '../src/deploy/prepare.js';
import {runtimeInfrastructure} from '../src/deploy/infrastructure.js';
import {removeArtifacts} from '../src/connect/artifacts.js';
import {shared} from '../src/connect/shared.js';
const infra={vpc_id:'vpc-abcd1234',subnet_ids:['subnet-abcd1234','subnet-cdef1234'],merchant_security_group:'sg-abcd1234',task_role:'task',execution_role:'execution',database_secret_ref:'arn:aws:secretsmanager:us-east-2:123456789012:secret:database-abc'};
const fixture=()=>mkdtempSync(join(realpathSync(tmpdir()),'auteric-deploy-'));
test('unknown hosting produces a removable continuation without fake enrollment',async()=>{
 const root=fixture();try {
  const plan=await prepareDeployment(root,{domain:'new-store.example'});
  assert.equal(plan.status,'deployment_preparation_required');assert.equal(plan.continuation.stop_for_user,false);
  assert.equal(plan.deployment,null);assert.ok(plan.blockers.some(b=>b.code==='deployment_platform_unknown'));
  assert.equal(existsSync(join(root,'auteric/runtime-connection.json')),false);
  const result=await removeArtifacts(root);assert.ok(result.removed.includes('auteric/deployment-plan.json'));
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('historical ephemeral storage blocks preparation without changing merchant task',async()=>{
 const root=fixture();try {
  mkdirSync(join(root,'deploy'));const bytes=JSON.stringify({containerDefinitions:[{name:'store',environment:[{name:'DATABASE_PATH',value:'/tmp/store.sqlite'}]}]});
  writeFileSync(join(root,'deploy/task-definition.json'),bytes);
  const plan=await prepareDeployment(root,{domain:'shop.example',environment:'staging'});
  assert.ok(plan.blockers.some(b=>b.code==='merchant_storage_not_durable'));assert.ok(plan.blockers.some(b=>b.code==='live_task_not_verified'));
  assert.equal(readFileSync(join(root,'deploy/task-definition.json'),'utf8'),bytes);assert.equal(plan.deployment,null);
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('owned plan refreshes during continuation but merchant edits are preserved',async()=>{
 const root=fixture();try {
  await prepareDeployment(root,{domain:'shop.example'});
  const next=await prepareDeployment(root,{domain:'shop.example',environment:'staging'});
  assert.equal(next.blockers.some(b=>b.code==='environment_unconfirmed'),false);
  const path=join(root,'auteric/deployment-plan.json');writeFileSync(path,'merchant edit');
  await assert.rejects(prepareDeployment(root,{domain:'shop.example'}),/merchant edited/);
  assert.equal(readFileSync(path,'utf8'),'merchant edit');
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('runtime infrastructure creates installation secrets only, with scoped access',()=>{
 const out=runtimeInfrastructure(infra);
 assert.equal(out.Metadata.Auteric.runtime_storage,'gateway/v1');
 assert.equal(out.Resources.ApplicationToken.Properties.GenerateSecretString.PasswordLength,64);
 assert.deepEqual(Object.values(out.Resources).map(r=>r.Type).sort(),['AWS::IAM::Policy','AWS::IAM::Policy','AWS::SecretsManager::Secret','AWS::SecretsManager::Secret']);
 assert.equal(JSON.stringify(out).includes('"Resource":"*"'),false);
 assert.equal(JSON.stringify(out).includes('RuntimeDatabase'),false);
});
test('runtime database provisioning is rejected, networking is not required',()=>{
 assert.throws(()=>runtimeInfrastructure({...infra,create_database:true}),/requires no PostgreSQL/);
 assert.ok(runtimeInfrastructure({task_role:'task',execution_role:'execution'}).Outputs.EnrollmentSecret);
 assert.throws(()=>runtimeInfrastructure({task_role:'task'}),/role names required/);
});
test('verified durable deployment context generates the enrollment input automatically',async()=>{
 const root=fixture();try {
  mkdirSync(join(root,'deploy'));mkdirSync(join(root,'auteric/.state'),{recursive:true,mode:0o700});
  writeFileSync(join(root,'deploy/task-definition.json'),JSON.stringify({containerDefinitions:[{name:'store',environment:[{name:'DATABASE_PATH',value:'/data/store.sqlite'}],mountPoints:[{containerPath:'/data',sourceVolume:'merchant-data'}]}]}));
  const release=(await shared('release')).bundledRelease();
  const deployment={schema:'auteric-deployment/v1',platform:'ecs-fargate',runtime_image_digest:release.image || 'registry.example/runtime@sha256:'+'a'.repeat(64),architecture:'linux/amd64',secret_ref:infra.database_secret_ref,state_ref:{profile:'gateway/v1',reference:'installation'},discovery_mount:'discovery',network_ref:'vpc-abcd1234',merchant_service:'store',runtime_port:7080,task_definition:'deploy/task-definition.json',application_secret_ref:infra.database_secret_ref,adapter_mount:'auteric-integration'};
  writeFileSync(join(root,'auteric/.state/deployment-context.json'),JSON.stringify({live_task_verified:true,deployment}));
  const plan=await prepareDeployment(root,{domain:'shop.example',environment:'staging'});
  assert.equal(plan.status,'enrollment_pending');assert.deepEqual(plan.blockers,[]);
  assert.equal(plan.deployment,'auteric/deployment.json');
  assert.deepEqual(JSON.parse(readFileSync(join(root,plan.deployment))),deployment);
 }finally{rmSync(root,{recursive:true,force:true});}
});
