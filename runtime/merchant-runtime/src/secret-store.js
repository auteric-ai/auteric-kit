import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { SecretsManagerClient, GetSecretValueCommand, PutSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { atomicWrite } from './atomic.js';

/** The task role, not owner credentials, reads and rotates one existing secret. */
export function serviceSecret(reference,{client,field='auteric_runtime_credential'}={}) {
  if (reference?.startsWith('file:/')) return {
    read:async()=>JSON.parse(readFileSync(reference.slice(5),'utf8')),
    save:async value=>atomicWrite(reference.slice(5),JSON.stringify(value)),
  };
  const match=/^arn:aws:secretsmanager:([a-z0-9-]+):\d{12}:secret:[A-Za-z0-9_/-]+$/.exec(reference||'');
  if(!match)throw Error('runtime identity must use a persistent file or existing Secrets Manager ARN');
  client ||= new SecretsManagerClient({region:match[1]});
  const current=async()=>{
    const response=await client.send(new GetSecretValueCommand({SecretId:reference}));
    if(typeof response.SecretString!=='string')throw Error('runtime secret must be JSON');
    return JSON.parse(response.SecretString);
  };
  return {
    async read(){const raw=await current();return raw[field]||raw;},
    async save(value){
      const raw=await current();
      const existing=raw[field] || (raw.token?raw:null);
      if(existing?.installation_id && existing.installation_id!==value.installation_id)throw Error('secret belongs to another installation');
      const next=raw.token?value:{...raw,[field]:value};
      await client.send(new PutSecretValueCommand({SecretId:reference,SecretString:JSON.stringify(next),
        ClientRequestToken:createHash('sha256').update(JSON.stringify(next)).digest('hex')}));
    },
  };
}
