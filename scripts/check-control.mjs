import {readFileSync} from 'node:fs';
import {randomBytes,createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';

// ECS can briefly have no healthy target while replacing a single replica.
// Only read-only probes retry; incompatibility and ambiguous writes still fail.
export async function checkControl(release, {
 base='https://control.auteric.com', fetchImpl=fetch,
 pause=ms=>new Promise(resolve=>setTimeout(resolve,ms)), attempts=24,
 log=message=>console.error(message),
}={}) {
 async function request(path,options={}) {
  const retryable=(options.method || 'GET')==='GET';
  for(let attempt=0; ;attempt++) {
   let response;
   try {
    response=await fetchImpl(base+path,{...options,signal:AbortSignal.timeout(20000)});
   } catch(error) {
    if(!retryable || attempt>=attempts-1)throw error;
   }
   if(response?.ok)return response.json();
   if(response && (!retryable || ![408,429,500,502,503,504].includes(response.status) || attempt>=attempts-1))
    throw Error(`Control ${path}: ${response.status}`);
   log(`Control ${path}: ${response?.status ?? 'network error'}; retry ${attempt+1}/${attempts-1}`);
   await pause(Math.min(20000,2000*(attempt+1)));
  }
 }
 await request('/ready');
 const runtime=await request('/api/commerce/runtime-compatibility');
 if(runtime.runtime_state_protocol!=='auteric-runtime-state/v1' || runtime.available!==true || runtime.registry_digest!==release.registry_digest || runtime.protocol!==release.control_api || !runtime.binding_kinds?.includes('auteric-module-binding/v1') || !runtime.environments?.includes('production'))throw Error('Control and the published Kit are incompatible');
 const verifier=randomBytes(32).toString('base64url');
 const auth=await request('/api/commerce/cli/start',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({challenge:createHash('sha256').update(verifier).digest('base64url'),state:randomBytes(32).toString('base64url'),approval_mode:'browser'})});
 if(auth.approval_mode!=='browser')throw Error('Browser approval mode is incompatible');
 const anonymous=await fetchImpl(base+'/api/commerce/stores/not-owned/installations',{method:'POST',headers:{'content-type':'application/json','x-auteric-console':'1'},body:JSON.stringify({environment:'production',endpoint:'https://qualification.example',transport:'native_http'}),signal:AbortSignal.timeout(20000)});
 if(![401,403].includes(anonymous.status))throw Error('Owner authorization is not enforced');
 return {status:'passed',scope:'live_public_compatibility',registry_digest:runtime.registry_digest,operations:runtime.operations,owner_approval_required:true,merchant_deployed:false};
}

if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
 const release=JSON.parse(readFileSync(new URL('../release-manifest.json',import.meta.url)));
 console.log(JSON.stringify(await checkControl(release)));
}
