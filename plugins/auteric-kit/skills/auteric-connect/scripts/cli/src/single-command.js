import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {existsSync,readFileSync,openSync,closeSync,mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {installDiscoveryRoute} from './sidecar-discovery.js';

async function availablePort(){const server=createServer();await new Promise((done,fail)=>{server.once('error',fail);server.listen(0,'127.0.0.1',done);});const port=server.address().port;await new Promise(done=>server.close(done));return port;}
function child(command,args,cwd,env,log){const fd=openSync(log,'a',0o600);const process=spawn(command,args,{cwd,env,detached:true,stdio:['ignore',fd,fd]});closeSync(fd);process.on('error',()=>{});return process;}
async function stop(process){if(!process?.pid||process.exitCode!==null)return;try{globalThis.process.kill(-process.pid,'SIGTERM');}catch{}await Promise.race([new Promise(done=>process.once('exit',done)),delay(3000)]);if(process.exitCode===null)try{globalThis.process.kill(-process.pid,'SIGKILL');}catch{}}
export async function temporaryTunnel(url,root){
 const log=join(root,'.auteric','tunnel-'+new URL(url).port+'.log');
 const tunnel=child(process.env.AUTERIC_CLOUDFLARED || 'cloudflared',['tunnel','--url',url,'--no-autoupdate'],root,process.env,log);
 try{
  for(let i=0;i<120;i++){
   if(tunnel.exitCode!==null)throw Error('Temporary HTTPS tunnel stopped; inspect its private log');
   const match=existsSync(log)&&readFileSync(log,'utf8').match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/);
   if(match)return {url:match[0],stop:()=>stop(tunnel)};
   await delay(250);
  }
  throw Error('Temporary HTTPS tunnel did not become ready');
 }catch(error){await stop(tunnel);throw error;}
}

// Start only an unambiguously supported Node application using its own command.
// Existing credentials, unresolved actions and merchant state are never cleared.
export async function withApplicationRuntime(root,layout,options,plan,run){
 const backend=layout.backend;mkdirSync(join(backend,'.auteric'),{recursive:true,mode:0o700});
 const children=[],tunnels=[];
 try{
  // The process must load the generated JSON route on its first start.
  installDiscoveryRoute(backend,layout.frontend);
  // The installation contract pins this private task-local endpoint. Check it
  // before browser pairing instead of silently choosing an untrusted port.
  const bridgeProbe=createServer();
  await new Promise((done,fail)=>{bridgeProbe.once('error',()=>fail(Error('The required private Bridge port 3101 is already in use; existing runtime was preserved')));bridgeProbe.listen(3101,'127.0.0.1',done);});
  await new Promise(done=>bridgeProbe.close(done));
  if(!options['application-url']){
   const pkg=JSON.parse(readFileSync(join(backend,'package.json'),'utf8'));
   if(!pkg.scripts?.start)throw Error('Automatic application startup requires the merchant start script or --application-url');
   if(!existsSync(join(backend,'node_modules')))await new Promise((done,fail)=>{const install=spawn('npm',['ci','--workspaces=false'],{cwd:backend,stdio:'inherit'});install.once('error',fail);install.once('exit',code=>code===0?done():fail(Error('Merchant dependency installation failed')));});
   const port=options['application-port'] || process.env.PORT || await availablePort();
   options['application-url']='http://127.0.0.1:'+port;
   const app=child('npm',['run','start'],backend,{...process.env,PORT:String(port)},join(backend,'.auteric','application.log'));children.push(app);
   const route=plan.bindings.find(b=>b.operation==='search_products').route.path;
   let ready=false;
   for(let i=0;i<240;i++){
    if(app.exitCode!==null)throw Error('Merchant start script stopped; inspect its private log');
    try{const r=await fetch(options['application-url']+route,{signal:AbortSignal.timeout(500)});if(r.ok&&r.headers.get('content-type')?.includes('application/json')){ready=true;break;}}catch{}
    await delay(250);
   }
   if(!ready)throw Error('Merchant application did not become ready on the selected PORT');
  }
  if(!options['sidecar-public-url']){
   const local=options['sidecar-url'] || 'http://127.0.0.1:'+await availablePort();
   options['sidecar-url']=local;
   const tunnel=await temporaryTunnel(local,backend);tunnels.push(tunnel);options['sidecar-public-url']=tunnel.url;
   console.log('Temporary Sidecar HTTPS: '+tunnel.url+'. The Bridge remains loopback only.');
  }
  return await run({...options,sidecar:true,serve:true,environment:options.environment || 'staging'});
 }finally{for(const tunnel of tunnels)await tunnel.stop();for(const process of children)await stop(process);}
}

// A tunnel URL being allocated does not prove the origin is reachable. Probe
// only liveness here; never retry a commerce action to wait for the edge.
export async function waitForSidecarIngress(endpoint, child, {timeoutMs=60000}={}) {
 const url=new URL('/health/live',endpoint);
 const deadline=Date.now()+timeoutMs;
 while(Date.now()<deadline) {
  if(child.exitCode!==null)throw Error('Sidecar stopped before public ingress became live');
  try {
   const response=await fetch(url,{redirect:'error',headers:{'cache-control':'no-cache'},signal:AbortSignal.timeout(Math.min(2500,Math.max(1,deadline-Date.now())))});
   if(response.ok && response.headers.get('content-type')?.includes('application/json')) {
    const body=await response.json();
    if(body.status==='live' && body.merchant_protocol==='1')return;
   }
  }catch{}
  await delay(Math.min(500,Math.max(1,deadline-Date.now())));
 }
 throw Error('Public Sidecar ingress did not return verified liveness; no connection-test action was submitted');
}
