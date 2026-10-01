import { readFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { inventoryRepo } from '../inventory/index.js';
import { shared } from './shared.js';
import { selectMapping } from './mapping.js';
import { writeArtifacts, initializeManaged } from './artifacts.js';
import { MANAGED_STATE, managedPath } from './layout.js';
import { testBridge } from './test-bridge.js';
import { renderECS } from '../deploy/ecs.js';
import { renderCompose } from '../deploy/compose.js';
import { credentialTarget, deliverCredential } from './credentials.js';
import { connectionStatus, atomicJSON, readJSON } from '../workflow.js';

export function prepareResult(root, status, extra={}) {
  const saved=readJSON(managedPath(root,'config.json'));
  const previous=saved?.status==='disconnected'?{}:(saved||{});
  const state={...previous,integration:status,status,tested_operations:[],production_ready:false,...extra};
  initializeManaged(root);
  atomicJSON(managedPath(root,'config.json'),state);
  connectionStatus(root,{phase:'preparation',status,outcome:'incomplete',production_ready:false,...extra},MANAGED_STATE);
  return state;
}
export async function prepareHTTP(root,options,{layout,base,domain,authenticate,request,dryRun=false,moduleBinding,localReport}={}) {
  const started=performance.now();
  options={...options};
  if(!options.mapping && existsSync(join(root,'auteric/mapping.json')))options.mapping='auteric/mapping.json';
  if(!options.deployment && existsSync(join(root,'auteric/deployment.json')))options.deployment='auteric/deployment.json';
  if(existsSync(managedPath(root,'connect-options.json')))options={...JSON.parse(readFileSync(managedPath(root,'connect-options.json'),'utf8')),...options};
  if(!dryRun)initializeManaged(root);
  const {digest,validateDocument,validateConnection}=await shared('mapping');const {bundledRelease,qualifyRelease}=await shared('release');
  const inventory=moduleBinding ? {graph:{nodes:[]},budget:{skipped:{}},candidates:[]} : await inventoryRepo(layout.backend,{backendDir:options.backend});
  // Inventory stays local. Only the selected mapping and digests can leave this process.
  const classified=(inventory.graph?.nodes||[]).flatMap(n=>n.routes||[]).map(r=>({method:r.method,path:r.path,
    classification:/admin|auth|session|webhook/i.test(r.path)?'internal':/payment|checkout|refund/i.test(r.path)?'excluded_from_mvp':'candidate'}));
  if(Object.entries(inventory.budget.skipped).some(([name,count])=>/^over_/.test(name)&&count))throw Error('inventory_incomplete: local scan budget exceeded; select a smaller authoritative backend');
  if(!dryRun)atomicJSON(managedPath(root,'http-inventory.json'),{routes:classified,registry:inventory.registry,operation_candidates:inventory.candidates.map(c=>({operation:c.operation,status:'candidate',routes:c.evidence?.entrypoints || []}))});
  const mapping=moduleBinding ? {mapping:moduleBinding,source:'model_adapter'} : await selectMapping(layout.backend,{
    ...options,...(options.mapping?{mapping:resolve(root,options.mapping)}:{}),
  },inventory);
  const registry=JSON.parse(readFileSync(new URL('../../runtime/contracts/registry/registry.json',import.meta.url),'utf8'));
  if(digest(registry)!==mapping.mapping.registry_digest)throw Error('runtime_api_incompatible: bundled operation registry digest mismatch');
  if(dryRun)return {status:'prepared_preview',mapping_digest:digest(mapping.mapping),operations:mapping.mapping.operations.map(o=>o.operation),routes:classified};
  atomicJSON(managedPath(root,'http-inventory.json'),{routes:classified,source:mapping.source});
  if(!options.deployment)throw Error('deployment_prerequisite_missing: provide existing storage/network/secret references with --deployment PATH');
  const deployment=validateDocument('deployment',JSON.parse(readFileSync(resolve(root,options.deployment),'utf8')));
  const release=bundledRelease();
  // Qualification precedes any browser pairing, Store mutation or merchant artifact.
  qualifyRelease(release,deployment);
  if(deployment.platform==='compose' && resolve(deployment.discovery_mount)!==join(resolve(root),'auteric/discovery'))
    throw Error('deployment_prerequisite_missing: discovery_mount must be the project auteric/discovery directory');
  const compatible=await request(base,'/api/commerce/runtime-compatibility');
  if(moduleBinding && !compatible.binding_kinds?.includes('auteric-module-binding/v1'))throw Error('runtime_api_incompatible: Control does not advertise module adapter enrollment');
  if(compatible.protocol!==release.control_api||compatible.available!==true||compatible.registry_digest!==release.registry_digest)throw Error('runtime_api_incompatible: install a qualified shared bootstrap provider before connecting');
  if(!options.environment)throw Error('environment_required: select --environment dev|sandbox|staging|production explicitly');
  const secretPath=await credentialTarget(root,deployment);
  const connectionFile=moduleBinding ? 'auteric/runtime-connection.json' : 'auteric/connection.json';
  const previous=readJSON(join(root,connectionFile));
  const merchantOrigin=options['backend-url']||`https://${domain}`;
  if(previous) {
    validateConnection(previous,{privateHosts:[deployment.merchant_service]});
    if(previous.mapping_digest!==digest(mapping.mapping)||previous.domain!==domain||previous.environment!==options.environment
       ||previous.runtime_release!==release.version||previous.control_origin!==base||previous.backend.origin!==merchantOrigin)throw Error('artifact_conflict: existing installation mapping/environment/release changed; review and revalidate explicitly');
    const priorState=readJSON(managedPath(root,'config.json'));
    if(priorState?.runtime_enrollment && priorState.installation_id===previous.installation_id) {
      const json=v=>JSON.stringify(v,null,2)+'\n';
      const files={[connectionFile]:json(previous)};
      if(deployment.platform==='compose')files['auteric/compose.yaml']=await renderCompose(previous,deployment,release);
      else {
        files['auteric/deploy.json']=json(deployment);
        if(moduleBinding) {
          if(!deployment.task_definition)throw Error('deployment_prerequisite_missing: model must identify the existing ECS task definition');
          const rendered=await renderECS(readJSON(resolve(root,deployment.task_definition)),previous,deployment,release);
          files['auteric/task-definition.json']=json(rendered.taskDefinition);
        }
      }
      const unchanged=await writeArtifacts(root,files,{dryRun:true});
      const saved=secretPath.startsWith('arn:')?await (await shared('secret-store')).serviceSecret(secretPath).read():JSON.parse(readFileSync(secretPath,'utf8'));
      if(!unchanged.no_op||saved.installation_id!==previous.installation_id)throw Error('artifact_conflict: installation artifacts or service identity missing; reconcile explicitly');
      // Local preparation is unchanged. No buyer/cart mutation, new identity
      // or authoritative runtime-health claim is made by a local no-op.
      return {...priorState,no_op:true,needs_reconciliation:true,next_action:'Preparation is unchanged; check current deployed runtime and Control evidence.'};
    }
  }
  if(!moduleBinding && (!options['test-query']||!options['test-currency']||!options['test-origin']))throw Error('bridge_test_required: select an isolated loopback test backend, product query and currency through --test-origin, --test-query and --test-currency');
  if(moduleBinding && (localReport?.adapter_fingerprint!==moduleBinding.adapter_fingerprint || JSON.stringify([...(localReport?.operations||[])].sort())!==JSON.stringify(moduleBinding.operations.map(op=>op.operation).sort())))
    throw Error('bridge_test_failed: local acceptance does not match this exact adapter/subset');
  const report=moduleBinding ? localReport : await testBridge(mapping.mapping,{origin:options['test-origin'],installationId:'test_'+digest(mapping.mapping).slice(7,23),statePath:managedPath(root,'bridge-test.sqlite'),query:options['test-query'],currency:options['test-currency']});
  atomicJSON(managedPath(root,'bridge-test.json'),report);
  if(report.status!=='passed')throw Error('bridge_test_failed: '+report.reason);
  const {auth,store}=await authenticate();
  if(store.environment!==options.environment)throw Error('Store environment does not match explicitly selected environment');
  // The deployed engine binds all operation executions to this exact
  // reviewed installation mapping. The compiled registry stays separately pinned.
  const remote=await request(base,`/api/commerce/stores/${store.id}/installations`,{token:auth.access_token});
  if(remote.installations?.some(item=>item.environment===store.environment&&!item.revoked_at))throw Error('installation_conflict: an active remote installation exists; reconcile it or disconnect it explicitly before replacing its mappings');
  const policy=await request(base,`/api/commerce/stores/${store.id}/policy`,{token:auth.access_token});
  await request(base,`/api/commerce/stores/${store.id}/policy`,{method:'PUT',token:auth.access_token,body:policy});
  const registration=await request(base,`/api/commerce/stores/${store.id}/installations`,{method:'POST',token:auth.access_token,body:{environment:store.environment,transport:'native_http',endpoint:`https://${domain}`,release_id:release.version,protocol_version:'1',native_runtime:{binding_digest:digest(mapping.mapping)},sidecar:{integration_version:release.version,storage_mode:'durable',storage_backend:deployment.state_ref.profile==='postgresql/v1'?'postgres':'sqlite',agent_ingress:true,allowed_operations:mapping.mapping.operations.map(op=>op.operation),profiles:Object.fromEntries(mapping.mapping.operations.map(op=>[op.operation,{operation:op.operation,enabled:false,mapping_fingerprint:moduleBinding ? moduleBinding.adapter_fingerprint : digest(op),integration_mode:'service_bridge',merchant_selection_id:'connect:'+digest(mapping.mapping).slice(7,31),target:{base_url:'http://127.0.0.1:3101',method:'POST',path:'/invoke/'+op.operation,auth_scheme:'bearer',credential_ref:'env:AUTERIC_BRIDGE_TOKEN',credential_header:'authorization'}}]))},operations:mapping.mapping.operations.map(op=>({operation:op.operation,contract_digest:release.registry_digest,binding_digest:digest(mapping.mapping)}))}});
  const connection={schema:'auteric-connection/v1',domain,environment:store.environment,control_origin:base,store_id:store.id,installation_id:registration.id,runtime_release:release.version,backend:{transport:'http',origin:merchantOrigin},mapping_digest:digest(mapping.mapping),mapping:mapping.mapping};
  const json=v=>JSON.stringify(v,null,2)+'\n';
  const files={[connectionFile]:json(connection)};
  if(deployment.platform==='compose')files['auteric/compose.yaml']=await renderCompose(connection,deployment,release);
  else {
    files['auteric/deploy.json']=json(deployment);
    if(moduleBinding) {
      if(!deployment.task_definition)throw Error('deployment_prerequisite_missing: model must identify the existing ECS task definition');
      const rendered=await renderECS(readJSON(resolve(root,deployment.task_definition)),connection,deployment,release);
      files['auteric/task-definition.json']=json(rendered.taskDefinition);
    }
  }
  // No UCP is invented while enrollment is pending. Runtime publishes exact Control-issued bytes after bootstrap.
  await writeArtifacts(root,files,{dryRun:true});
  const enrolled=await request(base,`/api/commerce/stores/${store.id}/runtime-enrollment`,{method:'POST',token:auth.access_token,body:{installation_id:registration.id,mapping_digest:connection.mapping_digest,mapping:mapping.mapping}});
  await deliverCredential(secretPath,enrolled);
  await writeArtifacts(root,files);
  return prepareResult(root,'deployment_pending',{...(moduleBinding?{integration:'module',local_only:false,tested_operations:localReport.operations}:{}),runtime_enrollment:true,deployment_platform:deployment.platform,store_id:store.id,installation_id:registration.id,mapping_digest:connection.mapping_digest,candidate_operations:mapping.mapping.operations.map(o=>o.operation),api_url:base,domain,next_action:'Run the merchant deployment workflow; scoped verification follows deployment',preparation_ms:Math.round(performance.now()-started)});
}
