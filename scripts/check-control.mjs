import {readFileSync} from 'node:fs';
import {randomBytes,createHash} from 'node:crypto';
const release=JSON.parse(readFileSync(new URL('../release-manifest.json',import.meta.url)));
const base='https://control.auteric.com';
async function request(path,options={}) {
 const response=await fetch(base+path,{...options,signal:AbortSignal.timeout(20000)});
 if(!response.ok)throw Error(`Control ${path}: ${response.status}`);
 return response.json();
}
const ready=await request('/ready');
const runtime=await request('/api/commerce/runtime-compatibility');
if(runtime.available!==true || runtime.registry_digest!==release.registry_digest || runtime.protocol!==release.control_api || !runtime.binding_kinds.includes('auteric-module-binding/v1') || !runtime.environments.includes('production'))throw Error('Control and the published Kit are incompatible');
const verifier=randomBytes(32).toString('base64url');
const auth=await request('/api/commerce/cli/start',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({challenge:createHash('sha256').update(verifier).digest('base64url'),state:randomBytes(32).toString('base64url'),approval_mode:'browser'})});
if(auth.approval_mode!=='browser')throw Error('Browser approval mode is incompatible');
const anonymous=await fetch(base+'/api/commerce/stores/not-owned/installations',{method:'POST',headers:{'content-type':'application/json','x-auteric-console':'1'},body:JSON.stringify({environment:'production',endpoint:'https://qualification.example',transport:'native_http'}),signal:AbortSignal.timeout(20000)});
if(![401,403].includes(anonymous.status))throw Error('Owner authorization is not enforced');
console.log(JSON.stringify({status:'passed',scope:'live_public_compatibility',registry_digest:runtime.registry_digest,operations:runtime.operations,owner_approval_required:true,merchant_deployed:false}));
