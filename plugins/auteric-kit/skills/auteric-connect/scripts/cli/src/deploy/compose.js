import { shared } from '../connect/shared.js';
import { dirname,basename } from 'node:path';
import { createHash } from 'node:crypto';
export async function renderCompose(connection, deployment, release) {
  const { validateConnection }=await shared('mapping');
  const { qualifyRelease }=await shared('release');
  validateConnection(connection,{privateHosts:[deployment.merchant_service]});qualifyRelease(release,deployment);
  if(deployment.platform!=='compose' || deployment.state_ref.profile!=='gateway/v1') throw Error('Compose requires Gateway-backed runtime state');
  if(!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(deployment.network_ref)) throw Error('Compose needs existing named private network');
  if(!deployment.secret_ref.startsWith('file:/')) throw Error('Compose service credential must reference an existing mounted secret file');
  const isModule=connection.mapping.schema==='auteric-module-binding/v1';
  if(isModule && !deployment.application_secret_ref?.startsWith('file:/'))throw Error('private application secret file required');
  const q=JSON.stringify, identityDirectory=dirname(deployment.secret_ref.slice(5)),identityName=basename(deployment.secret_ref.slice(5));
  if(basename(identityDirectory)!=='identity')throw Error('Compose credential requires a dedicated private identity directory for atomic rotation');

  const project='auteric-'+createHash('sha256').update(connection.installation_id).digest('hex').slice(0,16);
  return `name: ${q(project)}
services:
  auteric-runtime:
    image: ${q(deployment.runtime_image_digest)}
    platform: ${q(deployment.architecture)}
    user: ${q(deployment.runtime_user)}
    restart: unless-stopped
    init: true
    read_only: true
    tmpfs: ["/tmp"]
    environment:
      AUTERIC_CONNECTION: /etc/auteric/runtime-connection.json${isModule ? '\n      AUTERIC_ADAPTER_CONNECTION: /integration/connection.json\n      AUTERIC_APPLICATION_TOKEN_FILE: /run/secrets/auteric-application' : ''}
      AUTERIC_SERVICE_SECRET: file:/run/auteric-identity/${identityName}
      AUTERIC_STATE: /tmp/auteric
      AUTERIC_DISCOVERY: /tmp/auteric-discovery/ucp
      AUTERIC_RUNTIME_PORT: ${q(String(deployment.runtime_port))}
    volumes:
      - ${isModule ? './runtime-connection.json' : './connection.json'}:/etc/auteric/runtime-connection.json:ro${isModule ? '\n      - .:/integration:ro' : ''}
      - ${q(identityDirectory)}:/run/auteric-identity:rw
${isModule ? '    secrets: [auteric-application]' : ''}
    networks: [auteric-private]
    expose: [${q(String(deployment.runtime_port))}]
    healthcheck:
      test: ["CMD", "node", "/app/src/healthcheck.js"]
      interval: 15s
      timeout: 5s
      retries: 3
    mem_limit: 512m
    cpus: 0.5
    security_opt: ["no-new-privileges:true"]
    cap_drop: [ALL]
networks:
  auteric-private:
    external: true
    name: ${q(deployment.network_ref)}
${isModule ? 'secrets:\n  auteric-application:\n    file: '+q(deployment.application_secret_ref.slice(5)) : ''}
`;
}
