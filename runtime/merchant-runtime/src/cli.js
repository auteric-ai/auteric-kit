import { readFileSync } from 'node:fs';
import { manage } from './manager.js';
import { retainServiceIdentity } from './identity.js';
import { validateConnection } from './mapping.js';
import { bundledRelease } from './release.js';
import { serviceSecret } from './secret-store.js';
import { managerState } from './manager-state.js';
if (process.argv.includes('--local-acceptance')) {
  await (await import('./local-runtime.js')).startLocalRuntime();
} else {
if(process.env.AUTERIC_APPLICATION_TOKEN_FILE)process.env.AUTERIC_APPLICATION_TOKEN=readFileSync(process.env.AUTERIC_APPLICATION_TOKEN_FILE,'utf8').trim();
const connection=JSON.parse(process.env.AUTERIC_CONNECTION_JSON || readFileSync(process.env.AUTERIC_CONNECTION,'utf8'));
validateConnection(connection,{privateHosts:[new URL(connection.backend.origin).hostname]});
const release=bundledRelease();
if(connection.runtime_release!==release.version || connection.mapping.registry_digest!==release.registry_digest)
  throw Error('runtime release or registry incompatibility');
const secret=process.env.AUTERIC_SERVICE_SECRET;
const databaseUrl=process.env.AUTERIC_STATE_DATABASE_URL;
let credentialPath,secretStore,durableState;
if(secret?.startsWith('file:/')) credentialPath=retainServiceIdentity(process.env.AUTERIC_STATE,JSON.parse(readFileSync(secret.slice(5),'utf8')),connection.installation_id);
else {
  if(!databaseUrl)throw Error('ECS runtime requires PostgreSQL for durable translation and uncertainty state');
  secretStore=serviceSecret(secret); durableState=managerState(databaseUrl,connection.installation_id);
}
const manager=await manage(connection,{statePath:process.env.AUTERIC_STATE,secretPath:credentialPath,secretStore,durableState,databaseUrl,discoveryPath:process.env.AUTERIC_DISCOVERY,port:Number(process.env.AUTERIC_RUNTIME_PORT||7080)});
process.once('SIGTERM',()=>void manager.close().finally(()=>durableState?.close()));process.once('SIGINT',()=>void manager.close().finally(()=>durableState?.close()));
}
