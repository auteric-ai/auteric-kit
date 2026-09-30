// Supported single-host pilot using the existing binder, MEP runtime and test lifecycle.
import { stripTypeScriptTypes } from 'node:module';
import { existsSync, mkdirSync, readFileSync, writeFileSync, cpSync, openSync, closeSync } from 'node:fs';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { installDiscoveryRoute } from './sidecar-discovery.js';
import { bindRepo } from './binding/index.js';
import { atomicJSON, readJSON } from './workflow.js';
import { loadOperationsRegistry } from './inventory/operations.js';
import { applicationPlan } from './application-plan.js';
import { applicationAdapters } from './application-bridge.js';

export const MINIMUM_OPERATIONS = ['search_products','get_product','create_cart','get_cart','add_to_cart'];
const kit = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function origin(value, loopback = false) {
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
      (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1','localhost','[::1]'].includes(url.hostname))))
    throw Error('Expected HTTPS origin or explicit loopback HTTP');
  if (loopback && !['127.0.0.1','localhost','[::1]'].includes(url.hostname)) throw Error('This pilot Sidecar must share the merchant host');
  return url.origin;
}

// Public routing and local binding are separate; the Bridge remains loopback.
export function sidecarEndpoints(base, options = {}) {
  const local = origin(options['sidecar-url'] || 'http://127.0.0.1:8089', true);
  if (new URL(local).protocol !== 'http:') throw Error('Sidecar local binding requires loopback HTTP');
  const control = new URL(base);
  const localControl = ['127.0.0.1','localhost','[::1]'].includes(control.hostname);
  const endpoint = options['sidecar-public-url'] ? origin(options['sidecar-public-url']) : local;
  if (!localControl && (new URL(endpoint).protocol !== 'https:' || ['127.0.0.1','localhost','[::1]'].includes(new URL(endpoint).hostname)))
    throw Error('Hosted Control requires --sidecar-public-url https://YOUR_SIDECAR_HOST routed to this Sidecar; the private Bridge must never be exposed');
  return { local, endpoint };
}

export async function processCommand(command, args, cwd, env = process.env) {
  await new Promise((done, failed) => {
    const child = spawn(command, args, { cwd, env, stdio: 'inherit' });
    child.once('error', failed); child.once('exit', code => code === 0 ? done() : failed(Error(`${command} exited ${code}`)));
  });
}

export async function pythonEnvironment(root) {
  const venv = join(root, '.auteric', 'runtime-venv');
  const python = join(venv, 'bin', 'python');
  const digest=readJSON(join(kit,'runtime','pilot-manifest.json'))?.digest;
  const stamp=join(venv,'bundle.json');
  if (!existsSync(python)) await processCommand(process.env.AUTERIC_PYTHON || 'python3', ['-m','venv',venv], root);
  if (readJSON(stamp)?.digest!==digest) {
    await processCommand(python, ['-m','pip','install','--disable-pip-version-check',
      join(kit,'runtime','merchant-python')+'[sidecar,postgres]',
      join(kit,'runtime','pilot','packages','commerce-starter'),
      join(kit,'runtime','sdk'), 'beautifulsoup4', 'google-auth[requests]'], root);
    atomicJSON(stamp,{digest});
  }
  return python;
}

function start(command, args, cwd, env, logfile) {
  const fd = openSync(logfile, 'a', 0o600);
  const child=spawn(command,args,{cwd,env,stdio:['ignore',fd,fd]});closeSync(fd);
  child.on('error',()=>{});return child;
}
async function stop(child) {
  if (!child || child.exitCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([new Promise(done=>child.once('exit',done)),delay(5000)]);
  if (child.exitCode === null) child.kill('SIGKILL');
}
async function ready(url, child, headers = {}) {
  for (let i=0;i<60;i++) {
    if (child.exitCode !== null) throw Error('Runtime stopped; inspect private .auteric logs');
    try { const r = await fetch(url,{headers,signal:AbortSignal.timeout(1000)}); if (r.ok) return; } catch {}
    await delay(250);
  }
  throw Error('Runtime readiness deadline exceeded; inspect private .auteric logs');
}

function writeOwned(root,path,contents,records) {
  const target=join(root,path), previous=readJSON(join(root,'.auteric','sidecar-files.json')) || {};
  const hash=value=>createHash('sha256').update(value).digest('hex');
  if (existsSync(target) && readFileSync(target,'utf8')!==contents && previous[path]!==hash(readFileSync(target)))
    throw Error(`Preserved hand-edited integration file: ${path}`);
  mkdirSync(dirname(target),{recursive:true});writeFileSync(target,contents,{mode:0o644});records[path]=hash(contents);
}

export async function connectSidecar(root, options, context) {
  const {base,layout,project,domain,localSession,authenticatedStore,request,prepareDiscovery,
    provisionGatewayAccess,completeBrowserPairing,recordConnection,runProjectValidation} = context;
  const endpoints = sidecarEndpoints(base, options);
  const support = await request(base, '/api/commerce/direct-support');
  if (support.schema !== 'auteric-direct/v1') throw Error('Control does not support this released Sidecar integration');
  const scannerOrigin = options['scanner-url'] || 'https://scanner.auteric.com';
  let discoveryKey = options['discovery-key'];
  if (!discoveryKey) {
    if (new URL(base).protocol !== 'https:') throw Error('Local diagnostics require an independently pinned --discovery-key');
    if (!/^[A-Za-z0-9_-]{43}$/.test(support.discovery_trust?.public_key || '')) throw Error('Control did not supply its discovery verification key');
    discoveryKey = join(root, '.auteric', 'control-discovery-key.json');
    atomicJSON(discoveryKey, support.discovery_trust);
  }
  if (!options.serve) throw Error('The supported single-host Sidecar pilot requires --serve; no background deployment is implied');
  origin(scannerOrigin);
  if (!options.domain) throw Error('Public discovery acceptance requires --domain');
  if (typeof stripTypeScriptTypes!=='function') throw Error('The Sidecar pilot requires Node 22.13 or newer');
  const previousRun=readJSON(join(root,'.auteric','health-report.json'));
  if(previousRun?.state==='uncertain') throw Error('Resolve the retained uncertain action before another Connect; no automatic mutation retry');
  const backend=layout.backend;
  const pkg=JSON.parse(readFileSync(join(backend,'package.json'),'utf8'));
  if (pkg.type!=='module') throw Error('The initial Sidecar pilot supports Node ESM business services; other stacks retain their existing Connect workflows');
  const environment=options.environment || 'dev';
  if (!['dev','staging','sandbox'].includes(environment)) throw Error('The minimum Sidecar integration supports non-production stores only');
  const sidecarUrl=endpoints.local;
  const bridgePort=options['bridge-port'] || 3101;
  const adapterPaths=['server/auteric/index.ts','server/auteric/adapters/catalog.ts','server/auteric/adapters/cart.ts'];
  const existingAdapters=new Set(adapterPaths.filter(p=>existsSync(join(backend,p))));
  const appPlan=await applicationPlan(backend).catch(error=>{if(options['application-boundary'])throw error;return null;});
  const usesApplication=appPlan?.bindings.some(b=>b.evidence.business_symbol.factory);
  const operations=usesApplication?appPlan.bindings.map(b=>b.operation):MINIMUM_OPERATIONS;
  if(operations.length<MINIMUM_OPERATIONS.length && support.operation_subsets!==true)
    throw Error('This Control release does not support partial-operation acceptance; no capabilities were widened or activated');
  let plan,bindings;
  if(usesApplication){
    atomicJSON(join(backend,'.auteric','adapter-candidates.json'),appPlan);
    bindings=appPlan.bindings.map(b=>({...b,resource:b.operation.includes('product')?'catalog':'cart',symbol:{file:b.evidence.business_symbol.file}}));
  }else{
    plan=await bindRepo(backend,{approveCandidates:true,approvedBy:'merchant-connect',requireMerchantSelection:true,requiredServiceOperations:operations});
    bindings=plan.plan.bindings.filter(b=>operations.includes(b.operation));
  }
  for (const op of operations) {
    const b=bindings.find(b=>b.operation===op);
    if (!b || (!usesApplication && b.action!=='bind_service_call') || !/\.(m?js)$/.test(b.symbol?.file || ''))
      throw Error(`Integration required: ${op} needs a traced JavaScript business service with reviewed signature and canonical output. See .auteric/adapter-candidates.json`);
  }
  if (!usesApplication && !plan.validation.ok) throw Error('Existing binding validator rejected the adapters; inspect .auteric/installation.json');
  await runProjectValidation(layout,options);
  if(installDiscoveryRoute(backend,layout.frontend)) console.log('Added the public JSON discovery route. Restart the existing merchant server before public verification; commerce services are unchanged.');
  const owned=readJSON(join(backend,'.auteric','sidecar-files.json')) || {};
  for(const path of adapterPaths) if(!existingAdapters.has(path) && existsSync(join(backend,path))) owned[path]=createHash('sha256').update(readFileSync(join(backend,path))).digest('hex');
  cpSync(join(kit,'runtime','merchant-node'),join(backend,'.auteric','runtime','merchant-node'),{recursive:true});
  const compiled=join(backend,'.auteric','compiled');mkdirSync(compiled,{recursive:true});
  writeFileSync(join(compiled,'package.json'),' {"type":"module"}\n');
  const resources=[...new Set(bindings.map(b=>b.resource))];
  const imports=[],spreads=[];
  const bytes=[];
  for (const resource of usesApplication?[]:resources) {
    const file=bindings.find(b=>b.resource===resource).adapter_file;
    let source=readFileSync(join(backend,file),'utf8');bytes.push([file,source]);
    source=stripTypeScriptTypes(source,{mode:'strip'}).replace(/from (["'])(\.[^"']+)\1/g,(_m,_quote,spec)=>{
      const target=resolve(backend,dirname(file),spec);
      if (!target.startsWith(backend+'/')) throw Error('Adapter import escapes the merchant backend');
      return `from ${JSON.stringify(pathToFileURL(target).href)}`;
    });
    writeFileSync(join(compiled,resource+'.js'),source);
    imports.push(`import {${resource}Adapters} from './compiled/${resource}.js';`);
    spreads.push(`...${resource}Adapters()`);
  }
  for(const b of bindings) bytes.push([b.symbol.file,readFileSync(join(backend,b.symbol.file),'utf8')]);
  if(usesApplication){
    for(const b of bindings) bytes.push([b.evidence.entrypoints[0].file,readFileSync(join(backend,b.evidence.entrypoints[0].file),'utf8')]);
    writeOwned(backend,'auteric/application-bridge.mjs',readFileSync(join(kit,'src','application-bridge.js'),'utf8'),owned);
    writeOwned(backend,'auteric/application-binding.json',JSON.stringify(appPlan,null,2)+'\n',owned);
    bytes.push(['application-plan',JSON.stringify(appPlan)],['application-bridge',readFileSync(join(kit,'src','application-bridge.js'),'utf8')]);
  }
  const digest='sha256:'+createHash('sha256').update(JSON.stringify(bytes)).digest('hex');
  const bridge=`import {createServiceBridge} from './runtime/merchant-node/dist/bridge.js';\n${usesApplication?"import {applicationAdapters} from '../auteric/application-bridge.mjs';\nimport {MerchantError} from './runtime/merchant-node/dist/errors.js';\nimport {readFileSync} from 'node:fs';\nconst application=applicationAdapters(JSON.parse(readFileSync(new URL('../auteric/application-binding.json',import.meta.url),'utf8')),{origin:process.env.AUTERIC_APPLICATION_ORIGIN,statePath:new URL('./state/buyer-sessions.sqlite',import.meta.url).pathname,reject:code=>new MerchantError(code,'Merchant rejected request')});\nconst all=application.adapters;":imports.join('\n')+`\nconst all={${spreads.join(',')}};`}\nconst operations=${JSON.stringify(operations)};\nconst bridge=createServiceBridge({adapters:Object.fromEntries(operations.map(op=>[op,all[op]])),token:process.env.AUTERIC_BRIDGE_TOKEN,port:${bridgePort},host:'127.0.0.1'});\nawait bridge.listen();\nprocess.on('SIGTERM',()=>bridge.close().then(()=>process.exit(0)));\nprocess.on('SIGINT',()=>bridge.close().then(()=>process.exit(0)));\n`;
  writeOwned(backend,'.auteric/bridge.mjs',bridge,owned);atomicJSON(join(backend,'.auteric','sidecar-files.json'),owned);
  const python=await pythonEnvironment(backend);
  const {auth,store,state}=await authenticatedStore(base,root,domain,project,layout,{...options,environment},localSession);
  if(store.environment!==environment) throw Error('Existing Store environment differs; no installation was overwritten');
  const api='/api/commerce/stores/'+encodeURIComponent(store.id);
  const call=(path,body)=>request(base,api+path,{token:auth.access_token,...(body!==undefined?{method:'POST',body}:{})});
  const existing=await call('/installations');
  for(const item of existing.installations || []) {
    if(item.environment!==environment || item.revoked_at) continue;
    const current=item.trust_binding?.sidecar;
    if(item.id!==state.sidecar_installation_id || !current || current.storage_backend!=='sqlite' || (current.allowed_operations || []).some(op=>!operations.includes(op)))
      throw Error('An existing installation has a different or stronger integration. It was preserved; do not replace it with this pilot');
  }
  const health=await call('/connection-health');
  if(health.checks?.critical_errors?.state!=='active') throw Error('Retained unresolved writes block a new Connect test; inspect actions before retrying');
  const registry=loadOperationsRegistry();
  const registryIndex=JSON.parse(readFileSync(join(registry.path,'..','registry.json'),'utf8'));
  const canonical=value=>value && typeof value==='object' ? (Array.isArray(value)?'['+value.map(canonical).join(',')+']':'{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}') : JSON.stringify(value);
  const contractDigest='sha256:'+createHash('sha256').update(canonical(registryIndex)).digest('hex');
  const profiles=Object.fromEntries(bindings.map(b=>[b.operation,{operation:b.operation,enabled:false,
    mapping_fingerprint:'sha256:'+createHash('sha256').update(b.operation+digest).digest('hex'),
    integration_mode:'service_bridge',merchant_selection_id:'reviewed-'+b.operation,
    target:{base_url:'http://127.0.0.1:'+bridgePort,method:'POST',path:'/invoke/'+b.operation,
      auth_scheme:'bearer',credential_ref:'env:AUTERIC_BRIDGE_TOKEN',credential_header:'authorization'}}]));
  const installation=await call('/installations',{environment,transport:'native_http',endpoint:endpoints.endpoint,
    native_runtime:{binding_digest:digest},sdk_version:'0.1.0',release_id:digest.slice(7,31),
    operations:operations.map(operation=>({operation,contract_digest:contractDigest,binding_digest:digest})),
    sidecar:{integration_version:'sidecar-pilot-v1',profiles,allowed_operations:operations,
      storage_mode:'durable',storage_backend:'sqlite',agent_ingress:true}});
  state.sidecar_installation_id=installation.id;state.native_installation_id=installation.id;state.integration='sidecar';state.mode='single-host-pilot';
  atomicJSON(join(root,'.auteric','config.json'),state);
  const credential=await call('/sidecar/credential',{});
  const secretsPath=join(backend,'.auteric','sidecar-secrets.json');
  const secrets=readJSON(secretsPath) || {bridge:randomBytes(40).toString('base64url')};
  secrets.gateway=credential.token;atomicJSON(secretsPath,secrets,0o600);
  const env={...process.env,AUTERIC_BRIDGE_TOKEN:secrets.bridge,AUTERIC_SIDECAR_GATEWAY_TOKEN:secrets.gateway,AUTERIC_APPLICATION_ORIGIN:options['application-url']};
  const policy=await call('/policy');
  await request(base,api+'/policy',{method:'PUT',token:auth.access_token,body:policy});
  const configPath=join(backend,'.auteric','sidecar-runtime.json');
  async function configuration(){
    const config=await call(`/installations/${installation.id}/sidecar-config`);
    const operational=config.operational;
    operational.execution_store='sqlite:'+join(backend,'.auteric','state','executions.sqlite');
    operational.audit_store='sqlite:'+join(backend,'.auteric','state','audit.sqlite');
    mkdirSync(join(backend,'.auteric','state'),{recursive:true});
    atomicJSON(configPath,config,0o600);
  }
  await configuration();
  let bridgeChild,sidecarChild,stopping=false;
  const stopped=new Promise(done=>{
    const halt=()=>{stopping=true;void stop(sidecarChild);void stop(bridgeChild);done();};
    process.once('SIGINT',halt);process.once('SIGTERM',halt);
  });
  const startSidecar=()=>{if(stopping) throw Error('Connect stopped; retain any in-flight action for inspection');return start(python,['-m','auteric_merchant.sidecar_app','--config',configPath,'--host','127.0.0.1','--port',new URL(sidecarUrl).port || '8089'],backend,env,join(backend,'.auteric','sidecar.log'));};
  try {
    bridgeChild=start(process.execPath,[join(backend,'.auteric','bridge.mjs')],backend,env,join(backend,'.auteric','bridge.log'));
    await ready('http://127.0.0.1:'+bridgePort+'/health/ready',bridgeChild,{authorization:'Bearer '+secrets.bridge});
    sidecarChild=startSidecar();await ready(sidecarUrl+'/health/live',sidecarChild);
    if(!options['product-id'] && usesApplication){
      const local=applicationAdapters(appPlan,{origin:options['application-url'],statePath:join(backend,'.auteric','state','buyer-sessions.sqlite'),reject:code=>Error(code)});
      try {
        const found=await local.adapters.search_products({installationId:installation.id,principal:'catalog-selection',actionId:'catalog-selection'},{q:' ',limit:20});
        const safe=found.results.find(p=>p.variants.some(v=>v.available));
        if(!safe)throw Error('No live sellable merchant product is available for the connection test');
        options['product-id']=safe.product_id;options.query=options.query || safe.title;
      }finally{local.close();}
    }
    if(!options['product-id']) throw Error('Automatic test product selection is unavailable for this service adapter');
    recordConnection(root,'connection_test','running');
    let run=await call('/test-transaction?background=true',{bootstrap_sidecar:true,product_id:options['product-id'],query:options.query || options['product-id']});
    const deadline=Date.now()+120000;
    let last='';
    while(run.state==='running' && Date.now()<deadline){
      const now=run.stages.map(s=>s.name+':'+s.state).join(',');
      if(now!==last){console.log('Connection Test: '+now);last=now;}
      await delay(1000);run=await call(`/connection-tests/${run.run_id}`);
    }
    atomicJSON(join(root,'.auteric','health-report.json'),run);
    if(run.state!=='passed') throw Error(`Connection Test ${run.run_id}: ${run.state}. Inspect .auteric/health-report.json; uncertain writes must not retry`);
    await configuration();await stop(sidecarChild);sidecarChild=startSidecar();await ready(sidecarUrl+'/health/ready',sidecarChild);
    const profile=await request(base,'/ucp/'+store.id+'/.well-known/ucp');
    const previous=readJSON(join(root,'.auteric','config.json'));
    const published=prepareDiscovery(layout.frontend,project.framework,profile,{previousDigest:previous?.ucp_digest});
    state.ucp_digest=createHash('sha256').update(readFileSync(published.path)).digest('hex');
    atomicJSON(join(root,'.auteric','config.json'),state);
    if(options.domain) await call('/verify',{});
    await request(base,api+'/agent-access',{method:'PUT',token:auth.access_token,body:{enabled:true}});
    const mcp=await provisionGatewayAccess(root,base,domain,store.id,auth.access_token,operations,profile.auteric_mcp.endpoint);
    await processCommand(python,[join(kit,'src','public-agent.py'),'--domain',domain,'--key',resolve(discoveryKey),'--credential',mcp.credential_file,'--product-id',options['product-id'],'--query',options.query || options['product-id'],'--output',join(root,'.auteric','public-agent-evidence.json')],root);
    state.integration='sidecar';state.mode='single-host-pilot';state.status='runtime_verified';state.tested_operations=operations;
    state.connection_run_id=run.run_id;state.mcp_credential_file=mcp.credential_file;
    atomicJSON(join(root,'.auteric','config.json'),state);
    const scanner=origin(scannerOrigin);
    const scanStart=await request(scanner,'/api/scans',{method:'POST',body:{adapter:'generic',target_url:'https://'+domain}});
    let scan; const scanDeadline=Date.now()+120000;
    do {await delay(1000);scan=await request(scanner,'/api/scans/'+scanStart.scan_id);} while (!['completed','failed'].includes(scan.status) && Date.now()<scanDeadline);
    if(scan.status!=='completed') throw Error('Scanner did not complete the actual storefront scan');
    state.scanner_scan_id=scanStart.scan_id;
    run=await call(`/connection-tests/${run.run_id}/publish-evidence`,{});
    atomicJSON(join(root,'.auteric','health-report.json'),run);
    if(!run.scanner_evidence?.published) throw Error('Scanner did not accept current runtime evidence; minimum acceptance is incomplete');
    const currentHealth=await call('/connection-health');
    atomicJSON(join(root,'.auteric','health.json'),currentHealth);
    if(currentHealth.protection!=='active')throw Error('Current connection health is not active; inspect .auteric/health.json');
    state.status='minimum_verified';atomicJSON(join(root,'.auteric','config.json'),state);
    await completeBrowserPairing(base,auth,store.id);
    console.log(`Connection Test passed: ${run.run_id}. ${operations.length} verified capabilities use the private Bridge. Runtime is serving; Ctrl-C stops the added layer.`);
    recordConnection(root,'runtime','minimum_verified',{outcome:'minimum_verified',installation_status:'verified',verified_operations:operations,installation_id:installation.id});
    await Promise.race([stopped,new Promise(done=>{bridgeChild.once('exit',done);sidecarChild.once('exit',done);})]);
    return state;
  } finally {await stop(sidecarChild);await stop(bridgeChild);}
}

export async function pilot(root,options) {
  const python=await pythonEnvironment(root);
  const args=[join(kit,'src','pilot.py'),'--directory',join(root,'.auteric','pilot'),
    '--port',options.port || '8100'];
  if(options['mcp-url']) args.push('--mcp-url',origin(options['mcp-url']));
  console.log('Starting the existing local Gateway and Scanner. Open http://127.0.0.1:8090 for Scanner. This is a single-host pilot; Ctrl-C stops these services.');
  await processCommand(python,args,root);
}
