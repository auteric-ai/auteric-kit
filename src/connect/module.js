import { readFileSync, existsSync, writeFileSync, unlinkSync, readdirSync, rmSync, rmdirSync } from 'node:fs';
import { join, resolve, relative } from 'node:path';
import { inventoryRepo } from '../inventory/index.js';
import { atomicJSON, readJSON, safePath } from '../workflow.js';
import { initializeManaged, writeArtifacts, ownedArtifact } from './artifacts.js';
import { shared, runtimeRoot } from './shared.js';
import { disconnectHTTP } from './disconnect.js';

export async function integrationDossier(root) {
  const inventory = await inventoryRepo(root);
  const { OPERATIONS, REGISTRY_DIGEST } = await shared('contracts.generated');
  initializeManaged(root);
  const dossier = {schema:'auteric-model-dossier/v1',registry_digest:REGISTRY_DIGEST,inventory,
    capabilities:Object.entries(OPERATIONS).map(([operation,c]) => ({operation,version:c.contractVersion,path:c.path,side_effect:c.sideEffect})),
    instructions:'The current coding model must trace business/auth/transaction boundaries. Classify every capability as http_api, internal_service, unsupported, or implementation_required. Continue this same user request: read the source Skill and actual merchant code, generate adapter, connection and fixtures as an owned plan at auteric/.state/adapter-plan.json, then rerun the same Connect command internally. Do not ask the user to supply a plan or run another command. Connect auto-loads the plan and runs local acceptance. Do not copy/install an SDK, invent semantics, or launch a second coding agent.',
    runtime_source:runtimeRoot.href,adapter_interface:'auteric-adapter/v1: schema, merchant(services), projections[operation](raw, helpers), auth, session, errorMap, optional pagination[operation](raw). merchant callbacks are synchronous inside the application transaction; projections and helpers run in the generic runtime.',
  };
  atomicJSON(join(root,'auteric/.state/model-dossier.json'),dossier);
  const status={integration:'module',status:'implementation_required',local_only:true,production_ready:false,tested_operations:[]};
  atomicJSON(join(root,'auteric/.state/config.json'),status);
  atomicJSON(join(root,'auteric/.state/connection-status.json'),{...status,phase:'repository_inspection'});
  return dossier;
}

export async function installModule(root, plan) {
  const { REGISTRY_DIGEST, OPERATIONS } = await shared('contracts.generated');
  const config = JSON.parse(plan.files?.['auteric/connection.json'] || 'null');
  if (config?.schema !== 'auteric-module-connection/v1' || config.registry_digest !== REGISTRY_DIGEST)
    throw Error('generated adapter plan has incompatible contracts');
  for (const op of Object.keys(OPERATIONS)) {
    const item = plan.capabilities?.[op];
    if (!item || !['http_api','internal_service','unsupported','implementation_required'].includes(item.classification)
      || !item.reason || !Array.isArray(item.evidence)) throw Error('model capability classification missing: '+op);
    if (config.operations.includes(op) && !['http_api','internal_service'].includes(item.classification))
      throw Error('cannot bind an unsupported or unproven capability');
  }
  if (!Array.isArray(config.operations) || !Array.isArray(config.writes)
    || config.writes.some(op => !config.operations.includes(op))
    || config.operations.some(op => !OPERATIONS[op] || config.writes.includes(op) !== (OPERATIONS[op].sideEffect !== 'read')))
    throw Error('adapter operation effects differ from registry');
  const files = {...plan.files};
  if (config.application) files['auteric/private-hook.mjs'] = readFileSync(new URL('./private-hook.mjs',import.meta.url),'utf8');
  if (!existsSync(join(root,'auteric/.gitignore'))) files['auteric/.gitignore']='/.state/\n/discovery/\n';
  const changes = new Map();
  for (const patch of plan.hooks || []) {
    const path = safePath(resolve(root,patch.path));
    if (relative(root,path).startsWith('..') || patch.path.startsWith('auteric/') || !patch.before || !patch.after
      || patch.before === patch.after) throw Error('invalid owned application hook');
    const content = changes.get(path) ?? readFileSync(path,'utf8');
    if (content.split(patch.before).length !== 2) throw Error('hook source changed or ambiguous: '+patch.path);
    changes.set(path,content.replace(patch.before,patch.after));
  }
  await writeArtifacts(root,files,{dryRun:true});
  const hooksPath=join(root,'auteric/.state/hooks.json');
  if (existsSync(hooksPath)) throw Error('hook installation already exists; disconnect before replacing');
  initializeManaged(root);
  // Record ownership before mutations, including partial installs.
  atomicJSON(hooksPath,{schema:'auteric-owned-hooks/v1',hooks:plan.hooks || []});
  await writeArtifacts(root,files);
  for (const [path,content] of changes) writeFileSync(path,content);
  atomicJSON(join(root,'auteric/.state/capabilities.json'),plan.capabilities);
  atomicJSON(join(root,'auteric/.state/config.json'),{integration:'module',status:'prepared',local_only:true,production_ready:false});
  return (await shared('module-adapter')).loadAdapter(join(root,'auteric/connection.json'));
}

export async function disconnectModule(root, context = {}) {
  const directory=join(root,'auteric'), state=readJSON(join(directory,'.state/config.json')) || {};
  const ledger=readJSON(join(directory,'.state/artifacts.json')) || {};
  const patches=readJSON(join(directory,'.state/hooks.json'))?.hooks || [];
  const changes=new Map();
  for (const name of Object.keys(ledger)) if (existsSync(join(root,name)) && !await ownedArtifact(root,name))
    throw Error('disconnect_cleanup_required: owned artifact was edited: '+name);
  for (const patch of [...patches].reverse()) {
    const path=safePath(resolve(root,patch.path));
    if(relative(root,path).startsWith('..')) throw Error('hook escapes merchant root');
    const content=changes.get(path) ?? readFileSync(path,'utf8');
    if(content.split(patch.after).length !== 2) throw Error('disconnect_cleanup_required: owned hook was edited: '+patch.path);
    changes.set(path,content.replace(patch.after,patch.before));
  }
  if (state.runtime_enrollment) await disconnectHTTP(root,state,context);
  if (state.local_runtime_running) throw Error('stop the owned local acceptance process before disconnect');
  for (const [path,content] of changes) writeFileSync(path,content);
  for (const name of Object.keys(ledger)) if(existsSync(join(root,name))) unlinkSync(join(root,name));
  if(state.local_only) rmSync(join(directory,'.state'),{recursive:true,force:true});
  for (const sub of ['tests','discovery']) if(existsSync(join(directory,sub)) && !readdirSync(join(directory,sub)).length) rmdirSync(join(directory,sub));
  if(existsSync(directory) && !readdirSync(directory).length) rmdirSync(directory);
  return {status:'disconnected',removed:Object.keys(ledger),hooks_reversed:patches.length};
}
