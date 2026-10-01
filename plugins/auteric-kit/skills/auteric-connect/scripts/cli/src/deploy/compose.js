import { shared } from '../connect/shared.js';
import { createHash } from 'node:crypto';
export async function renderCompose(connection, deployment, release) {
  const { validateConnection }=await shared('mapping');
  const { qualifyRelease }=await shared('release');
  validateConnection(connection,{privateHosts:[deployment.merchant_service]});qualifyRelease(release,deployment);
  if(deployment.platform!=='compose' || deployment.state_ref.profile!=='sqlite-local-volume/v1') throw Error('Compose requires qualified local persistent SQLite volume');
  if(!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(deployment.network_ref) || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(deployment.state_ref.reference)) throw Error('Compose needs existing named private network and state volume');
  if(!deployment.secret_ref.startsWith('file:/')) throw Error('Compose service credential must reference an existing mounted secret file');
  const isModule=connection.mapping.schema==='auteric-module-binding/v1';
  if(isModule && !deployment.application_secret_ref?.startsWith('file:/'))throw Error('private application secret file required');
  const q=JSON.stringify;

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
      AUTERIC_SERVICE_SECRET: file:/run/secrets/auteric-service
      AUTERIC_STATE: /data
      AUTERIC_DISCOVERY: /discovery/ucp
      AUTERIC_RUNTIME_PORT: ${q(String(deployment.runtime_port))}
    volumes:
      - ${isModule ? './runtime-connection.json' : './connection.json'}:/etc/auteric/runtime-connection.json:ro${isModule ? '\n      - .:/integration:ro' : ''}
      - auteric-state:/data
      - ${q(deployment.discovery_mount + ':/discovery')}
    secrets: [auteric-service${isModule ? ', auteric-application' : ''}]
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
volumes:
  auteric-state:
    external: true
    name: ${q(deployment.state_ref.reference)}
secrets:
  auteric-service:
    file: ${q(deployment.secret_ref.slice(5))}${isModule ? '\n  auteric-application:\n    file: '+q(deployment.application_secret_ref.slice(5)) : ''}
`;
}
