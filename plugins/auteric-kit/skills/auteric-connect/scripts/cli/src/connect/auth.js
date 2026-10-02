import {randomBytes,createHash} from 'node:crypto';
import {existsSync,unlinkSync,lstatSync} from 'node:fs';
import {sessionPath,cachedSession,atomicJSON,readJSON} from '../workflow.js';

/** One bounded authorization step. Never hold a model turn for a browser timeout. */
export async function ownerSession(root,base,domain,options,{request,openBrowser,now=()=>Date.now()}) {
  const path=sessionPath(root,base,domain),pendingPath=path+'.pending';
  const cached=cachedSession(path);
  if(cached) {
    try {await request(base,'/api/commerce/auth/me',{token:cached.access_token});return {authorized:true};}
    catch(error) {if(!/^(401|403)\b/.test(error.message))throw error;unlinkSync(path);}
  }
  let pending=readJSON(pendingPath),started=false;
  if(pending && lstatSync(pendingPath).mode & 0o077)throw Error('Authorization continuation must be private');
  if(pending && (!Number.isFinite(pending.expires_at)||typeof pending.verifier!=='string'||typeof pending.state!=='string'))throw Error('Invalid authorization continuation');
  if(!pending||pending.expires_at*1000<=now()) {
    const verifier=randomBytes(32).toString('base64url'),state=randomBytes(32).toString('base64url');
    const challenge=createHash('sha256').update(verifier).digest('base64url');
    const session=await request(base,'/api/commerce/cli/start',{method:'POST',body:{challenge,state,approval_mode:options['no-browser']?'device':'browser'}});
    const link=new URL(session.authorization_url);
    if(link.origin!==base||link.pathname!=='/cli/authorize')throw Error('Unexpected authorization URL');
    pending={...session,verifier,state};atomicJSON(pendingPath,pending);started=true;
    if(!options['no-browser'])openBrowser(link.href);
  }
  if(!started) {
    const result=await request(base,'/api/commerce/cli/poll',{method:'POST',body:{request_id:pending.request_id,state:pending.state,verifier:pending.verifier}});
    if(result.status==='authorized') {
      atomicJSON(path,{...result,expires_at:now()+Math.max(0,Number(result.expires_in||0)-60)*1000});unlinkSync(pendingPath);return {authorized:true};
    }
  }
  return {authorized:false,status:'authentication_pending',authorization_url:pending.authorization_url,expires_at:pending.expires_at,
    continuation:{owner:'merchant',stop_for_user:true,instructions:'Complete Control sign-in. Resume this same Connect request after authorization; approval is checked with Control, not inferred from a user message.'}};
}
