/** Fixed installation-scoped state operations, never arbitrary SQL over the network. */
export function remoteState({url,token,fetcher=fetch}) {
  const origin=new URL(url);
  if(origin.protocol!=='https:' && !(origin.protocol==='http:' && ['127.0.0.1','localhost'].includes(origin.hostname)))throw Error('HTTPS runtime state endpoint required');
  if(origin.username||origin.password||origin.search||origin.hash)throw Error('invalid runtime state URL');
  const call=async(operation,args)=>{
    const credential=typeof token==='function'?await token():token;
    const response=await fetcher(url,{method:'POST',redirect:'error',signal:AbortSignal.timeout(10000),headers:{authorization:'Bearer '+credential,'content-type':'application/json'},body:JSON.stringify({operation,arguments:args})});
    if(!response.ok)throw Error('Auteric runtime state unavailable: '+response.status);
    return response.json();
  };
  const query=async(sql,args=[])=>{
    const parameter=args.slice(1), command=sql.trim();let value;
    if(command.startsWith('SELECT cookie,expires FROM bridge_sessions'))value=await call('kv_get',['session',parameter]);
    else if(command.startsWith('INSERT INTO bridge_session_claims'))return call('kv_claim',['session_claim',parameter,true]);
    else if(command.startsWith('INSERT INTO bridge_sessions'))return call('kv_put',['session',[args[1]],{cookie:args[2],expires:args[3]}]);
    else if(command.startsWith('INSERT INTO bridge_ids'))return call('kv_put',['id',[args[1],args[3]],{original:args[2]}]);
    else if(command.startsWith('SELECT original FROM bridge_ids'))value=await call('kv_get',['id',parameter]);
    else if(command.startsWith('INSERT INTO bridge_variants'))return call('kv_put',['variant',parameter,true]);
    else if(command.startsWith('SELECT 1 FROM bridge_variants'))value=await call('kv_get',['variant',parameter]);
    else throw Error('Unsupported bridge state operation');
    return {rows:value.value===null?[]:[value.value],changes:0};
  };
  return {call,query,close:async()=>{}};
}
