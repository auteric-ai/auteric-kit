import { existsSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { atomicWrite, safeTarget } from './atomic.js';

/** A new enrollment gets its own identity file while retaining audit/ledger
 * state. Restarting the same enrollment preserves the latest rotated identity.
 */
export function retainServiceIdentity(statePath, bootstrap, installationId) {
  if(bootstrap.installation_id!==installationId || typeof bootstrap.token!=='string'
      || bootstrap.token.length<32 || !Number.isFinite(bootstrap.expires_at))
    throw Error('runtime bootstrap credential is invalid or belongs to another installation');
  const id=createHash('sha256').update(JSON.stringify([installationId,bootstrap.token])).digest('hex');
  const path=safeTarget(join(statePath,'service-identities',id,'identity.json'));
  if(!existsSync(path))atomicWrite(path,JSON.stringify(bootstrap));
  else if(JSON.parse(readFileSync(path,'utf8')).installation_id!==installationId)
    throw Error('persisted runtime identity belongs to another installation');
  return path;
}
