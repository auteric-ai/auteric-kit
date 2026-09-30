import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { inventoryRepo } from './inventory/index.js';

export const APPLICATION_OPERATIONS=['search_products','get_product','create_cart','get_cart','add_to_cart'];
export async function applicationPlan(root) {
  const report=await inventoryRepo(root);
  if(report.graph.conflicts.length || report.budget?.truncated) throw Error('Application inventory is ambiguous or incomplete');
  const routes=report.graph.nodes.flatMap(n=>n.routes);
  const issuers=routes.filter(r=>r.method==='POST' && r.auth.length===0 && /(?:session.*guest|guest.*session|anonymous.*session)/i.test(r.path));
  const bindings=[],decisions=[];
  for(const operation of APPLICATION_OPERATIONS) {
    const candidates=report.candidates.filter(c=>c.operation===operation && c.evidence.business_symbol && !c.reasons.some(r=>r.startsWith('ambiguous')));
    if(candidates.length!==1 || candidates[0].evidence.entrypoints.length!==1) {decisions.push({operation,reason:'No unambiguous traced merchant route'});continue;}
    const candidate=candidates[0],route=candidate.evidence.entrypoints[0];
    const actual=routes.find(r=>r.file===route.file && r.path===route.path && r.method===route.method);
    const auth=actual?.application_boundary?.auth;
    if(!actual || !['none','session'].includes(auth)) {decisions.push({operation,reason:'Unsupported authentication boundary'});continue;}
    const write=route.method!=='GET';
    if(write && (!actual.application_boundary.idempotent || auth!=='session' || issuers.length!==1)) {decisions.push({operation,reason:'Write requires an isolated guest session and merchant idempotency'});continue;}
    const source=readFileSync(join(root,route.file),'utf8').slice(actual.start_offset,actual.end_offset);
    const params=[...route.path.matchAll(/:([\w]+)/g)].map(m=>m[1]);
    const identity=operation==='get_product'?'product_id':'cart_id';
    if(params.length>1) {decisions.push({operation,reason:'Ambiguous path parameters'});continue;}
    const fields={};
    if(operation==='search_products') {
      const query=['query','q'].filter(n=>new RegExp(`\\.query\\.${n}\\b`).test(source));
      if(!query.length || !/\.query\.limit\b/.test(source)) {decisions.push({operation,reason:'Search input fields lack source evidence'});continue;}
      fields[query[0]]='q';fields.limit='limit';
    }
    if(operation==='add_to_cart') {
      const service=readFileSync(join(root,candidate.evidence.business_symbol.file),'utf8');
      const names=['variant_id','variantId'].filter(n=>new RegExp(`\\b[\\w$]+\\.${n}\\b`).test(service));
      if(names.length!==1 || !/\b[\w$]+\.quantity\b/.test(service)) {decisions.push({operation,reason:'Cart input mapping is unproven'});continue;}
      fields[names[0]]='variant_id';fields.quantity='quantity';
    }
    bindings.push({operation,route:{method:route.method,path:route.path},auth,idempotent:actual.application_boundary.idempotent,
      path_params:Object.fromEntries(params.map(p=>[p,identity])),fields,evidence:candidate.evidence});
  }
  // Partial cart lifecycles cannot be tested in isolation. Keep their candidates
  // visible with reasons but do not install or advertise them.
  if(!['get_product','create_cart','get_cart','add_to_cart'].every(op=>bindings.some(b=>b.operation===op))) {
    for(let i=bindings.length-1;i>=0;i--) if(!['search_products','get_product'].includes(bindings[i].operation)) decisions.push({operation:bindings.splice(i,1)[0].operation,reason:'Complete isolated cart acceptance lifecycle is unavailable'});
  }
  if(!bindings.some(b=>b.operation==='search_products')) throw Error('No supported catalog search application boundary');
  return {schema:'auteric-application-binding/v1',bindings,decisions,session:issuers.length===1?{method:issuers[0].method,path:issuers[0].path}:null};
}
