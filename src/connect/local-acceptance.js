import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtempSync, readFileSync, writeFileSync, existsSync, lstatSync, cpSync, mkdirSync, readdirSync, rmSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomBytes, createHash } from 'node:crypto';
import { runtimeRoot, shared } from './shared.js';
import { bundledHarness } from './harness.js';
import { buildReport, operationRecord } from '../acceptance/report.js';
import { loadVectors, buildScenarioPlan } from '../acceptance/scenarios.js';
import { loadOperationsRegistry } from '../inventory/operations.js';
import { atomicJSON, readJSON } from '../workflow.js';

const pause=ms=>new Promise(ok=>setTimeout(ok,ms));
async function port() { const server=createServer(); await new Promise(ok=>server.listen(0,'127.0.0.1',ok));
  const value=server.address().port; await new Promise(ok=>server.close(ok)); return value; }
async function ready(url, child) { for(let n=0;n<100;n++) {
  if(child?.exitCode!==null && child?.exitCode!==undefined) throw Error('local process exited; inspect local logs');
  try {if((await fetch(url,{signal:AbortSignal.timeout(1000)})).ok)return;}catch{}
  await pause(100);
} throw Error('local readiness timed out: '+url); }

export function resolveRuntimeSource(root, options={}) {
  const valid=path=>existsSync(join(path,'services/commerce/app.py')) && existsSync(join(path,'packages/commerce-contracts/vectors/conformance'));
  const explicit=options['runtime-source'] || process.env.AUTERIC_RUNTIME_SOURCE;
  if(explicit) {
    const path=resolve(explicit);
    if(!valid(path))throw Error('runtime_source_required: selected Auteric source lacks the local Gateway and canonical test vectors');
    return path;
  }
  const bundledParent=fileURLToPath(new URL('../../',runtimeRoot));
  if(valid(bundledParent))return bundledParent;
  // A packaged CLI can reuse an existing sibling monorepo without embedding
  // server sources in the merchant or requiring another user-facing command.
  let parent=dirname(resolve(root));
  for(let depth=0;depth<3 && parent!==dirname(parent);depth++,parent=dirname(parent)) {
    if(valid(parent))return parent;
    const candidates=readdirSync(parent,{withFileTypes:true}).filter(entry=>entry.isDirectory() && !entry.name.startsWith('.'))
      .map(entry=>join(parent,entry.name)).filter(valid);
    const primary=candidates.filter(path=>existsSync(join(path,'.git')) && lstatSync(join(path,'.git')).isDirectory());
    if(primary.length===1)return primary[0];
    if(candidates.length===1)return candidates[0];
    if(candidates.length>1)throw Error('runtime_source_required: multiple Auteric sources; current model must select the compatible baseline');
  }
  throw Error('runtime_source_required: local contract acceptance requires an Auteric monorepo; the current model must locate it before continuing');
}

export function buildLocalImage(source) {
  const runtime=fileURLToPath(runtimeRoot), paths=['src','schemas','python','package.json','package-lock.json','release-manifest.json'];
  const sdk=join(source,'packages/merchant-python/src/auteric_merchant'), hash=createHash('sha256');
  function visit(path) { for(const name of readdirSync(path,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name))) {
    if(name.name==='__pycache__' || name.name.endsWith('.pyc')) continue;
    const file=join(path,name.name); if(name.isDirectory())visit(file);else hash.update(name.name).update(readFileSync(file));
  } }
  visit(join(runtime,'src'));visit(join(runtime,'schemas'));visit(join(runtime,'python'));
  for(const name of paths.slice(3))hash.update(readFileSync(join(runtime,name)));
  hash.update(readFileSync(join(runtime,'Dockerfile.local')));
  const tag='auteric-merchant-runtime:local-'+hash.digest('hex').slice(0,16);
  try {execFileSync('docker',['image','inspect',tag],{stdio:'ignore'});return tag;}catch{}
  const context=mkdtempSync(join(tmpdir(),'auteric-runtime-source-'));
  try {
    mkdirSync(join(context,'packages/merchant-runtime'),{recursive:true});
    for(const name of paths)cpSync(join(runtime,name),join(context,'packages/merchant-runtime',name),{recursive:true});
    execFileSync('docker',['build','--platform','linux/amd64','--pull=false','-f',join(runtime,'Dockerfile.local'),'-t',tag,context],{stdio:'inherit'});
    return tag;
  } finally {rmSync(context,{recursive:true,force:true});}
}

export async function localAcceptance(root, options={}) {
  const connection=join(root,'auteric/connection.json'), {config,fingerprint}=await (await shared('module-adapter')).loadAdapter(connection);
  const explicitSource=options['runtime-source'] || process.env.AUTERIC_RUNTIME_SOURCE;
  const context=explicitSource ? {source:resolveRuntimeSource(root,options)} : bundledHarness(options);
  const source=context.source;
  const sourceCommit=context.sourceCommit || execFileSync('git',['rev-parse','HEAD'],{cwd:source,encoding:'utf8'}).trim();
  if(options['runtime-commit'] && options['runtime-commit']!==sourceCommit)throw Error('runtime source commit differs from requested baseline');
  const release=(await shared('release')).bundledRelease();
  const image=options['runtime-image'] || (release.qualification?.passed && release.image ? release.image : buildLocalImage(source));
  try {execFileSync('docker',['image','inspect',image],{stdio:'ignore'});} catch {execFileSync('docker',['pull','--platform','linux/amd64',image],{stdio:'inherit'});}
  const imageId=JSON.parse(execFileSync('docker',['image','inspect',image],{encoding:'utf8'}))[0].Id;
  const state=join(root,'auteric/.state'), python=context.python || options.python || (existsSync(join(source,'.venv/bin/python')) ? join(source,'.venv/bin/python') : 'python3');
  const merchantPort=await port(), gatewayPort=await port(), sidecarPort=await port();
  const applicationToken=randomBytes(32).toString('hex'), bridgeToken=randomBytes(32).toString('hex'), adminToken=randomBytes(32).toString('hex');
  const {containedFile}=await shared('module-adapter');
  const fixture=await import(pathToFileURL(containedFile(dirname(connection),config.fixtures)).href);
  const appModule=await import(pathToFileURL(containedFile(root,config.application.module)).href);
  const name='auteric-local-'+randomBytes(6).toString('hex');let child, app, server;
  const statePath=join(state,'config.json'), status=readJSON(statePath) || {};
  const writeStatus=extra=>{
    const value={...status,...extra,local_only:true,integration:'module',production_ready:false};
    atomicJSON(statePath,value);atomicJSON(join(state,'connection-status.json'),{...value,phase:'local_acceptance'});
  };
  try {
    const previous=process.env.AUTERIC_APPLICATION_TOKEN;process.env.AUTERIC_APPLICATION_TOKEN=applicationToken;
    try {
      app=appModule[config.application.factory]({[config.application.databaseOption]:join(state,'merchant.sqlite'),[config.application.adminTokenOption]:adminToken});
    } finally {if(previous===undefined)delete process.env.AUTERIC_APPLICATION_TOKEN;else process.env.AUTERIC_APPLICATION_TOKEN=previous;}
    server=app.listen(merchantPort,'0.0.0.0');await new Promise((ok,bad)=>server.once('listening',ok).once('error',bad));
    const origin='http://127.0.0.1:'+merchantPort, selection=await fixture.select(origin,adminToken);
    const registry=loadOperationsRegistry(), vectorsDir=join(source,'packages/commerce-contracts/vectors/conformance');
    const negative_vectors=Object.fromEntries(config.operations.map(op=>[op,loadVectors(vectorsDir,op).negatives]));
    const canonical_scenarios=Object.fromEntries(config.operations.map(op=>[op,registry.operations[op].test_scenarios]));
    const scenario_plan=Object.fromEntries(config.operations.map(op=>[op,buildScenarioPlan(op,registry.operations[op],{vectorsDir,boundOperations:new Set(config.operations)})]));
    writeStatus({status:'testing',local_runtime_running:true});
    writeFileSync(join(state,'lab-input.json'),JSON.stringify({source,sourceCommit,imageId,merchantOrigin:origin,gatewayPort,sidecarPort,selection,adminToken,negative_vectors,canonical_scenarios,scenario_plan}),{mode:0o600});
    for(const name of ['lab-scope.json','runtime-ready.json','acceptance.json','sidecar.json'])rmSync(join(state,name),{force:true});
    const env={...process.env,PYTHONPATH:[source,fileURLToPath(new URL('../../runtime/sdk/src',import.meta.url)),join(source,'kits/auteric-kit/runtime/sdk/src'),join(source,'packages/merchant-python/src'),join(source,'packages/commerce-starter/src')].join(':')};
    let childError;
    child=spawn(python,[fileURLToPath(new URL('../acceptance/module-local.py',import.meta.url)),state],{env,stdio:['ignore','inherit','inherit']});
    child.once('error',error=>{childError=error;});
    for(let n=0;!existsSync(join(state,'lab-scope.json'));n++) {
      if(childError)throw childError;
      if(n>100 || child.exitCode!==null)throw Error('local Gateway preparation failed');await pause(100);
    }
    const scope=readJSON(join(state,'lab-scope.json'));
    const envFile=join(state,'runtime.env');
    writeFileSync(envFile,Object.entries({AUTERIC_CONNECTION:'/merchant/connection.json',AUTERIC_INSTALLATION_ID:scope.installation_id,
      AUTERIC_MERCHANT_ORIGIN:'http://host.docker.internal:'+merchantPort,AUTERIC_APPLICATION_TOKEN:applicationToken,
      AUTERIC_BRIDGE_TOKEN:bridgeToken,AUTERIC_CONTROL_TOKEN:scope.sidecar_token,AUTERIC_SIDECAR_GATEWAY_TOKEN:scope.sidecar_token,AUTERIC_RUNTIME_STATE_URL:`http://127.0.0.1:${gatewayPort}/api/commerce/stores/${scope.store_id}/runtime-state/${scope.installation_id}`,
      AUTERIC_SIDECAR_CONFIG:'/merchant/.state/sidecar.json',AUTERIC_LOCAL_GATEWAY_PORT:gatewayPort}).map(([k,v])=>k+'='+v).join('\n')+'\n',{mode:0o600});
    execFileSync('docker',['run','--platform','linux/amd64','-d','--name',name,...(process.platform==='linux'?['--add-host','host.docker.internal:host-gateway']:[]),'--user',process.getuid()+':'+process.getgid(),'--env-file',envFile,
      '-p','127.0.0.1:'+sidecarPort+':7070','-v',join(root,'auteric')+':/merchant:ro',image,'--local-acceptance'],{stdio:'pipe'});
    await ready('http://127.0.0.1:'+sidecarPort+'/health/live',child);
    atomicJSON(join(state,'runtime-ready.json'),{imageId,name});
    const code=child.exitCode ?? await new Promise(ok=>child.once('exit',ok));
    if(code!==0)throw Error('local acceptance failed; inspect auteric/.state/acceptance.json');
    const report=readJSON(join(state,'acceptance.json'));
    if(report?.status!=='passed')throw Error('local acceptance did not produce passing evidence');
    if((await (await shared('module-adapter')).loadAdapter(connection)).fingerprint!==fingerprint)throw Error('adapter changed during local acceptance');
    report.adapter_fingerprint=fingerprint;
    report.runtime_state_profile='gateway/v1';
    report.unsupported_operations=Object.keys(registry.operations).filter(op=>!report.operations.includes(op)).map(operation=>({operation,reason:'No tested adapter binding in this installation'}));
    atomicJSON(join(state,'acceptance.json'),report);
    const canonicalReport=buildReport({validation:{ok:true},operations:report.operations.map(op=>operationRecord(op,report.scenarios.filter(s=>s.operation===op)))});
    atomicJSON(join(state,'contract-report.json'),canonicalReport);
    atomicJSON(join(state,'deployment-preview.json'),{status:'deployment_pending',image:imageId,containers:1,adapter_mount:'./auteric:/merchant',
      merchant_hook_token:'required_private_secret_reference',production_ready:false,reason:'Merchant deployment and the public Connection Test remain pending; local acceptance does not activate public traffic.'});
    writeStatus({status:'local_verified',tested_operations:report.operations,local_runtime_running:false,image_id:imageId});
    return report;
  } catch(error) {
    writeStatus({status:'local_acceptance_failed',local_runtime_running:false});throw error;
  } finally {
    if(child && child.exitCode===null){child.kill('SIGTERM');await Promise.race([new Promise(ok=>child.once('exit',ok)),pause(3000)]);if(child.exitCode===null)child.kill('SIGKILL');}
    try{execFileSync('docker',['rm','-f',name],{stdio:'ignore'});}catch{}
    if(server)await new Promise(ok=>server.close(ok));app?.locals.close();
    const current=readJSON(statePath) || {};atomicJSON(statePath,{...current,local_runtime_running:false});
    for(const secret of ['runtime.env','lab-input.json','lab-scope.json'])if(existsSync(join(state,secret)))rmSync(join(state,secret));
  }
}
