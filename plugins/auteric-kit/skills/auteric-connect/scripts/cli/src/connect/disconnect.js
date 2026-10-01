import { join } from 'node:path';
import { atomicJSON, connectionStatus } from '../workflow.js';
import { managedPath, MANAGED_STATE } from './layout.js';
import { ownedArtifact, removeArtifacts } from './artifacts.js';

export async function disconnectHTTP(root,state,{authenticate,request,stopCompose}={}) {
  if(state.status==='disconnected')return {...state,no_op:true};
  const save=()=>atomicJSON(managedPath(root,'config.json'),state);
  // Revoke before stopping/cleaning, so a stale deployed copy cannot retain
  // access. Persist the boundary to resume after a local stop failure.
  try {
    if(state.runtime_enrollment && !state.remote_revoked) {
      state.status='disconnecting';state.production_ready=false;save();
      connectionStatus(root,{phase:'disconnect',status:'revoking',outcome:'pending',production_ready:false},MANAGED_STATE);
      const auth=await authenticate();
      await request(state.api_url,`/api/commerce/stores/${encodeURIComponent(state.store_id)}/installations/${encodeURIComponent(state.installation_id)}/revoke`,{method:'POST',token:auth.access_token});
      await request(state.api_url,`/api/commerce/stores/${encodeURIComponent(state.store_id)}/runtime-enrollment/${encodeURIComponent(state.installation_id)}`,{method:'DELETE',token:auth.access_token});
      state.remote_revoked=true;state.status='disconnecting';state.production_ready=false;save();
    }
    if(state.runtime_enrollment && state.deployment_platform==='compose' && !state.runtime_stopped) {
      if(!await ownedArtifact(root,'auteric/compose.yaml'))throw Error('disconnect_cleanup_required: Compose file is missing or edited; stop the installation runtime explicitly before removing it');
      await stopCompose(join(root,'auteric/compose.yaml'));
      state.runtime_stopped=true;save();
    } else if(state.runtime_enrollment && state.deployment_platform!=='compose' && !state.runtime_stopped) {
      throw Error('disconnect_cleanup_required: stop the runtime through the merchant deployment workflow; scoped access is revoked');
    }
    const cleanup=await removeArtifacts(root,state.installation_id);
    state.status='disconnected';state.integration='disconnected';state.disconnected_at=new Date().toISOString();
    state.production_ready=false;state.public_discovery_verified=false;state.local_cleanup_pending=false;state.cleanup=cleanup;save();
    connectionStatus(root,{phase:'disconnect',status:'disconnected',outcome:'disconnected',production_ready:false,cleanup},MANAGED_STATE);
    return state;
  } catch(error) {
    state.status='disconnecting';state.local_cleanup_pending=Boolean(state.remote_revoked);save();
    connectionStatus(root,{phase:'disconnect',status:state.remote_revoked?'cleanup_pending':'revocation_pending',outcome:'incomplete',production_ready:false},MANAGED_STATE);
    throw error;
  }
}
