/** PSP membership around the existing Store execution boundary. */
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { existsSync } from 'node:fs';
import { inventoryRepo } from '../inventory/index.js';
import { atomicJSON, readJSON, lockProject } from '../workflow.js';
import { initializeManaged, ownedArtifact, writeArtifacts } from './artifacts.js';

export const PSP_PROTOCOL = 'auteric-psp/v1';
const continuation = {
  owner:'current_coding_model', stop_for_user:false,
  reference:'plugins/auteric-kit/skills/auteric-connect/references/psp.md',
  instructions:'Inspect the authoritative PSP backend and authenticated merchant inventory. Prepare auteric/.state/psp-plan.json with real Control Store IDs and PSP merchant IDs. Reuse one adapter implementation and keep Store-scoped installations, policy, consent and evidence. Continue this same request. Never take merchant IDs from shopper inputs or treat membership as an access grant or live connection.',
};
const digest = value => 'sha256:' + createHash('sha256').update(JSON.stringify(value)).digest('hex');

export function validatePSPPlan(plan, options = {}) {
  const keys = ['schema','name','backend_origin','environment','platform_resource','stores'];
  if (!plan || plan.schema !== 'auteric-psp-plan/v1' || Object.keys(plan).some(k => !keys.includes(k)))
    throw Error('psp_plan_invalid: expected auteric-psp-plan/v1 without credentials');
  const origin = new URL(plan.backend_origin);
  if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash
      || origin.pathname !== '/' || (origin.port && origin.port !== '443')
      || origin.hostname === 'localhost' || /\.(?:localhost|local|internal)$/.test(origin.hostname))
    throw Error('psp_plan_invalid: backend_origin must be a public HTTPS origin');
  if (options.domain && options.domain.toLowerCase() !== origin.hostname)
    throw Error('psp_plan_invalid: preserve the requested PSP domain');
  if (!['dev','sandbox','staging','production'].includes(plan.environment)
      || (options.environment && options.environment !== plan.environment))
    throw Error('psp_plan_invalid: choose the matching environment explicitly');
  if (typeof plan.name !== 'string' || !plan.name.trim() || plan.name.length > 100)
    throw Error('psp_plan_invalid: name is required');
  if (plan.platform_resource !== undefined && (typeof plan.platform_resource !== 'string' || !plan.platform_resource))
    throw Error('psp_plan_invalid: platform_resource must identify a Platform');
  if (!Array.isArray(plan.stores) || !plan.stores.length || plan.stores.length > 1000)
    throw Error('psp_plan_invalid: select 1 to 1000 authorized Stores');
  const ids = new Set(), accounts = new Set();
  for (const row of plan.stores) {
    if (!row || Object.keys(row).some(k => !['store_id','merchant_id','name','domain'].includes(k))
        || (row.store_id !== undefined && (typeof row.store_id !== 'string' || !/^[A-Za-z0-9_-]{1,200}$/.test(row.store_id)))
        || (!row.store_id && (typeof row.name !== 'string' || !row.name.trim() || row.name.length > 100
          || typeof row.domain !== 'string' || !/^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$/i.test(row.domain)))
        || typeof row.merchant_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(row.merchant_id)
        || (row.store_id && ids.has(row.store_id)) || accounts.has(row.merchant_id))
      throw Error('psp_plan_invalid: Store and merchant IDs must be explicit and unique');
    ids.add(row.store_id); accounts.add(row.merchant_id);
  }
  if (!plan.platform_resource && plan.stores.some(row => row.store_id))
    throw Error('psp_plan_invalid: existing Stores require their actual Platform resource');
  return {...plan,backend_origin:origin.origin,stores:[...plan.stores].sort((a,b) => a.merchant_id.localeCompare(b.merchant_id))};
}

/** Context must have been verified by the existing merchant/runtime boundary. */
export function resolvePSPAccount(manifest, verifiedContext) {
  if (manifest?.schema !== 'auteric-psp-routing/v1' || !verifiedContext?.storeId)
    throw Error('verified Store execution context required');
  const binding = manifest.stores.find(row => row.store_id === verifiedContext.storeId);
  if (!binding || binding.psp_id !== manifest.psp_id || binding.environment !== manifest.environment)
    throw Error('Store is not bound to this PSP installation');
  return {store_id:binding.store_id,merchant_id:binding.merchant_id,
    backend_origin:binding.backend_origin,binding_digest:binding.binding_digest};
}

export async function connectPSP(root, options, {base,request,authorize,token,inventory=inventoryRepo}) {
  const raw = readJSON(join(root,'auteric/.state/psp-plan.json'));
  if (!raw) {
    const dossier = {schema:'auteric-psp-dossier/v1',domain:options.domain || null,
      inventory:await inventory(root,{backendDir:options.backend}),continuation};
    if (!options['dry-run']) {initializeManaged(root); atomicJSON(join(root,'auteric/.state/psp-dossier.json'),dossier);}
    return {status:'implementation_required',integration:'psp',production_ready:false,
      dossier:'auteric/.state/psp-dossier.json',plan:'auteric/.state/psp-plan.json',continuation};
  }
  const plan=validatePSPPlan(raw,options),fingerprint=digest(plan);
  if (options['dry-run']) return {status:'prepared_preview',integration:'psp',production_ready:false,
    backend_origin:plan.backend_origin,environment:plan.environment,stores:plan.stores,continuation};
  initializeManaged(root);
  const release=lockProject(root,'auteric/.state');
  try {
    const statePath=join(root,'auteric/.state/psp-onboarding.json'),prior=readJSON(statePath);
    if (prior && prior.plan_digest!==fingerprint) throw Error('artifact_conflict: PSP plan changed; reconcile registration explicitly');
    if (existsSync(join(root,'auteric/psp.json')) && !await ownedArtifact(root,'auteric/psp.json'))
      throw Error('artifact_conflict: preserve the edited PSP routing manifest');
    const compatibility=await request(base,'/api/commerce/psp-compatibility');
    if (compatibility.protocol!==PSP_PROTOCOL || compatibility.available!==true)
      throw Error('psp_api_incompatible: Control must support auteric-psp/v1');
    const auth=await authorize();
    if (!auth.authorized) return {...auth,integration:'psp',production_ready:false};
    const accessToken=await token(),identity=await request(base,'/api/commerce/auth/me',{token:accessToken});
    let pspId=plan.platform_resource || prior?.psp_id;
    if (!pspId) {
      const entity=await request(base,'/api/commerce/control/resources',{method:'POST',token:accessToken,
        body:{name:plan.name,kind:'platform',parent:identity.org}});
      pspId=entity.id;
      if (!pspId) throw Error('PSP Platform registration returned no identity');
      atomicJSON(statePath,{plan_digest:fingerprint,psp_id:pspId});
    }
    await request(base,'/api/commerce/control/psps/'+encodeURIComponent(pspId),{method:'PUT',token:accessToken,
      body:{backend_origin:plan.backend_origin,environment:plan.environment}});
    const bindings=[],createdStores={...(prior?.created_stores || {})};
    for (const row of plan.stores) {
      let storeId=row.store_id || createdStores[row.merchant_id];
      if (!storeId) {
        const created=await request(base,'/api/commerce/stores',{method:'POST',token:accessToken,
          body:{name:row.name,domain:row.domain,platform:'custom',environment:plan.environment,parent_id:pspId}});
        storeId=created.id;
        if (!storeId) throw Error('Store registration returned no identity');
        createdStores[row.merchant_id]=storeId;
        atomicJSON(statePath,{plan_digest:fingerprint,psp_id:pspId,created_stores:createdStores});
      }
      const response=await request(base,'/api/commerce/stores/'+encodeURIComponent(storeId)+'/psp',
        {method:'PUT',token:accessToken,body:{psp_id:pspId,merchant_id:row.merchant_id}});
      const binding=response.binding;
      if (!binding || binding.psp_id!==pspId || binding.store_id!==storeId
          || binding.merchant_id!==row.merchant_id || binding.backend_origin!==plan.backend_origin
          || binding.environment!==plan.environment || !/^sha256:[a-f0-9]{64}$/.test(binding.binding_digest))
        throw Error('PSP binding response differs from the reviewed mapping');
      bindings.push(binding);
    }
    const manifest={schema:'auteric-psp-routing/v1',psp_id:pspId,environment:plan.environment,stores:bindings,
      automatic_access_grants:false,shared_multi_store_runtime:false,payment_completion:false};
    await writeArtifacts(root,{'auteric/psp.json':JSON.stringify(manifest,null,2)+'\n'});
    atomicJSON(statePath,{plan_digest:fingerprint,psp_id:pspId,created_stores:createdStores,
      status:'membership_registered',store_ids:bindings.map(b=>b.store_id)});
    return {status:'membership_registered',integration:'psp',psp_id:pspId,stores:bindings,
      manifest:'auteric/psp.json',production_ready:false,continuation:{...continuation,
        instructions:'Membership is registered. Continue this request: implement and test the shared PSP adapter with two isolated merchant accounts, prepare Store-scoped runtime/connector deployments and wire the existing PSP deployment. Public discovery, activation and payment require real evidence. Do not stop at membership or advertise a single multi-Store Sidecar.'}};
  } finally {release();}
}
