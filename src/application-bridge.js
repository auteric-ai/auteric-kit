// Execute the existing HTTP boundary, never instantiate a factory outside its app.
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { chmodSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

function field(value, names) {
  const present = names.filter(name => value?.[name] !== undefined);
  if (!present.length) throw Error(`Merchant response lacks ${names.join('/')}`);
  const found = value[present[0]];
  if (present.some(name => JSON.stringify(value[name]) !== JSON.stringify(found))) throw Error('Conflicting merchant response aliases');
  return found;
}
function money(value, names, currency) {
  const amount = field(value, names);
  if (!Number.isSafeInteger(amount) || amount < 0 || !/^[A-Z]{3}$/.test(currency)) throw Error('Authoritative minor-unit money is required');
  return { amount_minor: amount, currency };
}
export function normalizeProduct(value) {
  if (!Array.isArray(value.variants) || !value.variants.length) throw Error('Authoritative variants are required');
  return { product_id: field(value, ['product_id','id']), title: field(value,['title','name']),
    ...(value.description !== undefined ? {description:value.description} : {}),
    variants: value.variants.map(v => ({variant_id:field(v,['variant_id','id']),
      price: v.price && typeof v.price === 'object' ? v.price : money(v,['priceCents','price_cents','amount_minor'],v.currency),
      available: typeof v.available === 'boolean' ? v.available :
        ['in_stock','out_of_stock'].includes(v.availability) ? v.availability === 'in_stock' : (()=>{throw Error('Variant availability is unknown');})()})) };
}
export function normalizeCart(value) {
  if (value.cart_id && Array.isArray(value.line_items)) return value;
  const currency = value.currency;
  if (!Array.isArray(value.items) || !['active','cancelled','converted'].includes(value.status)) throw Error('Authoritative cart state is required');
  return {cart_id:field(value,['cart_id','id']),resource_revision:field(value,['resource_revision','revision']),status:value.status,currency,
    line_items:value.items.map(item=>({line_id:field(item,['line_id','lineId']),product_id:field(item,['product_id','productId']),
      variant_id:field(item,['variant_id','variantId']),title:field(item,['title','name']),quantity:item.quantity,
      unit_price:money(item,['unitPriceCents','unit_price_cents'],item.currency),total_price:money(item,['lineTotalCents','total_price_cents'],item.currency)})),
    // A numeric aggregate without individually represented discounts cannot be
    // converted into invented discount identities.
    discounts: value.discounts || (field(value,['discountCents','discount_total_cents']) === 0 ? [] : (()=>{throw Error('Discount breakdown is required');})()),
    subtotal:money(value,['subtotalCents','subtotal_cents'],currency),discount_total:money(value,['discountCents','discount_total_cents'],currency),total:money(value,['totalCents','total_cents'],currency)};
}
export function normalizeResult(operation,value) {
  if(operation==='search_products') {
    const rows = Array.isArray(value) ? value : field(value,['results','products','items']);
    if(!Array.isArray(rows)) throw Error('Merchant search response is not a list');
    if(value.page && typeof value.page==='object') return {results:rows.map(normalizeProduct),page:value.page};
    const hasMore = Number.isInteger(value.totalPages) && Number.isInteger(value.page) ? value.page < value.totalPages :
      Number.isInteger(value.total) && Number.isInteger(value.limit) && Number.isInteger(value.page) ? value.page * value.limit < value.total : null;
    if(hasMore===null) throw Error('Search pagination cannot be inferred');
    return {results:rows.map(normalizeProduct),page:{has_more:hasMore,...(hasMore?{next_cursor:String(value.page+1)}:{})}};
  }
  if(operation==='get_product') return normalizeProduct(value);
  return normalizeCart(value);
}
export function applicationAdapters(plan,{origin,statePath,reject}={}) {
  const base = new URL(origin);
  if(base.protocol!=='http:' || !['127.0.0.1','localhost','[::1]'].includes(base.hostname)) throw Error('Merchant application transport must be loopback HTTP');
  mkdirSync(dirname(statePath),{recursive:true,mode:0o700});
  const db = new DatabaseSync(statePath);chmodSync(statePath,0o600);
  db.exec('CREATE TABLE IF NOT EXISTS buyer_sessions (subject TEXT PRIMARY KEY, cookie TEXT NOT NULL)');
  const pending = new Map();
  async function session(ctx) {
    // Bind both installation and pairwise buyer; all operations for this buyer
    // share its merchant cookie, while unrelated buyers never do.
    const subject=createHash('sha256').update(JSON.stringify([ctx.installationId,ctx.subject || ctx.principal])).digest('hex');
    const stored=db.prepare('SELECT cookie FROM buyer_sessions WHERE subject=?').get(subject);
    if(stored) return stored.cookie;
    if(!plan.session) throw Error('No verified anonymous session issuer');
    if(!pending.has(subject)) pending.set(subject,(async()=>{
      const r=await fetch(new URL(plan.session.path,base),{method:plan.session.method,redirect:'error',signal:AbortSignal.timeout(10000),headers:{'content-type':'application/json'},body:'{}'});
      if(!r.ok) throw Error('Merchant session issuance failed');
      const cookies=r.headers.getSetCookie().map(c=>c.split(';')[0]);
      if(cookies.length!==1 || !/^[\w-]+=[^;\r\n]+$/.test(cookies[0])) throw Error('Ambiguous merchant session cookie');
      db.prepare('INSERT INTO buyer_sessions VALUES(?,?)').run(subject,cookies[0]);return cookies[0];
    })().finally(()=>pending.delete(subject)));
    return pending.get(subject);
  }
  const adapters=Object.fromEntries(plan.bindings.map(binding=>[binding.operation,async(ctx,input)=>{
    const values={...input,...ctx.pathParams};
    let path=binding.route.path;
    for(const [name,canonical] of Object.entries(binding.path_params || {})) {
      if(typeof values[canonical]!=='string' || !values[canonical]) throw Error('Missing canonical resource identifier');
      path=path.replace(':'+name,encodeURIComponent(values[canonical]));
    }
    const url=new URL(path,base),headers={'content-type':'application/json'};
    if(binding.auth==='session') headers.cookie=await session(ctx);
    if(binding.idempotent) headers['idempotency-key']=ctx.actionId;
    const data={};
    for(const [name,canonical] of Object.entries(binding.fields)) if(values[canonical]!==undefined) data[name]=values[canonical];
    if(ctx.expectedRevision!==undefined && !binding.revision_field) throw Error('Merchant revision precondition is unsupported');
    if(binding.revision_field && ctx.expectedRevision!==undefined) data[binding.revision_field]=ctx.expectedRevision;
    if(binding.operation==='search_products' && values.cursor) {
      if(!/^[1-9]\d*$/.test(values.cursor)) throw Error('Unsupported merchant pagination cursor');
      data.page=Number(values.cursor);
    }
    const get=binding.route.method==='GET';
    if(get) for(const [k,v] of Object.entries(data)) url.searchParams.set(k,String(v));
    const response=await fetch(url,{method:binding.route.method,headers,redirect:'error',signal:AbortSignal.timeout(15000),...(get?{}:{body:JSON.stringify(data)})});
    if(!response.headers.get('content-type')?.includes('application/json')) throw Error('Merchant response must be JSON');
    if(!response.ok) {
      const failure=await response.json();
      const merchantCode=String(failure.code || failure.error?.code || '');
      const code=response.status===404?'RESOURCE_NOT_FOUND':response.status===401?'UNAUTHENTICATED':response.status===403?'FORBIDDEN':
        /IDEMPOTENCY.*CONFLICT/i.test(merchantCode)?'IDEMPOTENCY_CONFLICT':/REVISION.*CONFLICT/i.test(merchantCode)?'REVISION_CONFLICT':
          /(?:INSUFFICIENT|OUT_OF).*STOCK/i.test(merchantCode)?'OUT_OF_STOCK':'INVALID_INPUT';
      throw reject(code,response.status);
    }
    return normalizeResult(binding.operation,await response.json());
  }]));
  return {adapters,close:()=>db.close()};
}
