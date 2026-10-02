import { existsSync, lstatSync, readFileSync,mkdirSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { shared } from './shared.js';
import { initializeManaged } from './artifacts.js';

export async function credentialTarget(root,deployment) {
  if(deployment.platform==='ecs-fargate' && deployment.secret_ref.startsWith('arn:aws:secretsmanager:')) {
    const {serviceSecret}=await shared('secret-store');
    const current=await serviceSecret(deployment.secret_ref).read();
    if(current?.token && !current.installation_id)throw Error('existing secret contains an unrelated credential');
    return deployment.secret_ref;
  }
  if(deployment.platform!=='compose'||!deployment.secret_ref.startsWith('file:/'))throw Error('credential_delivery_required: a supported installation credential provider is required');
  const path=deployment.secret_ref.slice(5);
  if(resolve(path)!==join(resolve(root),'auteric/.state/identity/enrollment.json'))throw Error('deployment_prerequisite_missing: secret_ref must reference auteric/.state/identity/enrollment.json inside this project');
  const {safeTarget}=await shared('atomic');safeTarget(path);
  initializeManaged(root);
  mkdirSync(join(root,'auteric/.state/identity'),{recursive:true,mode:0o700});
  if(Number(deployment.runtime_user.split(':')[0])!==process.getuid())throw Error('deployment_prerequisite_missing: Compose runtime UID must match the enrollment file owner; retain the dedicated writable identity directory');
  if(!existsSync(path))return path;
  if(!lstatSync(path).isFile()||lstatSync(path).mode&0o077)throw Error('deployment_prerequisite_missing: enrollment secret must be a private regular file with mode 0600');
  if(lstatSync(path).uid!==Number(deployment.runtime_user.split(':')[0]))throw Error('deployment_prerequisite_missing: Compose runtime UID must own the enrollment file');
  const data=readFileSync(path,'utf8');
  if(data.trim()) {
    let parsed;try{parsed=JSON.parse(data);}catch{throw Error('existing secret is not an Auteric enrollment credential');}
    if(!parsed.installation_id||!parsed.token)throw Error('existing secret is not an Auteric enrollment credential');
  }
  return path;
}
export async function deliverCredential(path,enrollment) {
  if(path.startsWith('arn:aws:secretsmanager:')) {
    const {serviceSecret}=await shared('secret-store');const store=serviceSecret(path);
    const current=await store.read();
    if(current?.token && current.installation_id!==enrollment.installation_id)throw Error('secret belongs to a different installation');
    if(!enrollment.token){if(!current?.token)throw Error('enrollment credential recovery required');return;}
    if(current?.token)throw Error('existing service identity preserved; refusing silent replacement');
    await store.save({token:enrollment.token,expires_at:enrollment.expires_at,installation_id:enrollment.installation_id,enrollment:true});return;
  }
  const {atomicWrite}=await shared('atomic');
  const current=existsSync(path)?readFileSync(path,'utf8'):'';
  if(current.trim() && JSON.parse(current).installation_id!==enrollment.installation_id)throw Error('secret belongs to a different installation');
  if(!enrollment.token){if(!current.trim())throw Error('single-use enrollment credential was not retained; recover enrollment explicitly');return;}
  if(current.trim())throw Error('existing service identity preserved; refusing silent replacement');
  atomicWrite(path,JSON.stringify({token:enrollment.token,expires_at:enrollment.expires_at,installation_id:enrollment.installation_id,enrollment:true}));
}
