import { randomBytes } from 'node:crypto';
import { shared } from './shared.js';

/** Translation checks only; fixture contexts never count as Sidecar/Gateway enforcement evidence. */
export async function testBridge(mapping,{origin,installationId,statePath,query,currency}={}) {
  const base=new URL(origin);
  if(base.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(base.hostname))throw Error('Bridge mutations require an explicit isolated loopback merchant sandbox');
  if(!query||!currency)throw Error('choose explicit test query and authoritative currency');
  const {applicationAdapters}=await shared('application-bridge');const {createApplicationBridge}=await shared('bridge-server');
  const token=randomBytes(40).toString('base64url'),translator=applicationAdapters(mapping,{origin,installationId,statePath});
  const bridge=createApplicationBridge({adapters:translator.adapters,token});
  const address=await bridge.listen(),url=`http://127.0.0.1:${address.port}`;
  const results=[];
  const call=async(operation,input={}, {pathParams={},subject='test-buyer-a',actionId='action_'+randomBytes(16).toString('hex'),expectedRevision}={})=>{
    const context={installationId,operation,principal:subject,subject,actionId,pathParams,contractVersion:'1.0.0',...(expectedRevision===undefined?{}:{expectedRevision})};
    const response=await fetch(url+'/invoke/'+operation,{method:'POST',headers:{authorization:'Bearer '+token,'content-type':'application/json'},body:JSON.stringify({context,input}),signal:AbortSignal.timeout(15000)});
    return {status:response.status,value:await response.json()};
  };
  const expect=(condition,detail)=>{if(!condition)throw Error(detail);};
  try {
    const search=await call('search_products',{q:query});expect(search.status===200,'search output failed');
    results.push({operation:'search_products',status:'passed'});
    const product=search.value.results.find(p=>p.variants.some(v=>v.available));expect(product,'no authoritative available test variant');
    const detail=await call('get_product',{}, {pathParams:{product_id:product.product_id}});expect(detail.status===200,'product output failed');
    results.push({operation:'get_product',status:'passed'});
    const cart=await call('create_cart',{currency});expect(cart.status===200,'cart output failed');
    results.push({operation:'create_cart',status:'passed'});
    const pathParams={cart_id:cart.value.cart_id};
    const read=await call('get_cart',{}, {pathParams});expect(read.status===200,'cart read failed');
    const owner=await call('get_cart',{}, {pathParams,subject:'test-buyer-b'});expect([403,404].includes(owner.status),'buyer B could read buyer A cart');
    results.push({operation:'get_cart',status:'passed'});
    if(mapping.operations.some(op=>op.operation==='add_to_cart')) {
      const variant=product.variants.find(v=>v.available),revision=cart.value.resource_revision;
      const input={product_id:product.product_id,variant_id:variant.variant_id,quantity:1,expected_revision:revision},actionId='action_'+randomBytes(16).toString('hex');
      const first=await call('add_to_cart',input,{pathParams,expectedRevision:revision,actionId});expect(first.status===200,'cart add failed');
      const replay=await call('add_to_cart',input,{pathParams,expectedRevision:revision,actionId});expect(JSON.stringify(first.value)===JSON.stringify(replay.value),'same action duplicated mutation');
      const conflict=await call('add_to_cart',{...input,quantity:2},{pathParams,expectedRevision:revision,actionId});expect(conflict.value.error?.code==='IDEMPOTENCY_CONFLICT','changed-payload action not rejected');
      const stale=await call('add_to_cart',input,{pathParams,expectedRevision:revision});expect(stale.value.error?.code==='REVISION_CONFLICT','merchant HTTP route lacks atomic expected_revision');
      results.push({operation:'add_to_cart',status:'passed'});
    }
    return {schema:'auteric-bridge-test/v1',scope:'translation_only',enforcement_verified:false,operations:results,status:'passed'};
  }catch(error){return {schema:'auteric-bridge-test/v1',scope:'translation_only',enforcement_verified:false,operations:results,status:'failed',reason:error.message};}
  finally{await bridge.close();translator.close();}
}
