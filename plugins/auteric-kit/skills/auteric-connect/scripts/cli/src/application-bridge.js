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
  // Public MEP identifiers are stable aliases for actual merchant identities.
  // Keep the reverse mapping durable; a caller cannot invent an original ID.
  db.exec('CREATE TABLE IF NOT EXISTS merchant_ids (kind TEXT NOT NULL, original TEXT NOT NULL, canonical TEXT NOT NULL, PRIMARY KEY(kind, canonical), UNIQUE(kind, original))');
  function encodeId(kind,original) {
    if(typeof original!=='string' || !original || original.length>1024)throw Error('Authoritative merchant identifier is required');
    const canonical=kind+'_'+createHash('sha256').update(JSON.stringify([kind,original])).digest('hex');
    db.prepare('INSERT OR IGNORE INTO merchant_ids VALUES(?,?,?)').run(kind,original,canonical);
    if(db.prepare('SELECT original FROM merchant_ids WHERE kind=? AND canonical=?').get(kind,canonical)?.original!==original)throw Error('Merchant identifier collision');
    return canonical;
  }
  function decodeId(kind,canonical) {
    const row=db.prepare('SELECT original FROM merchant_ids WHERE kind=? AND canonical=?').get(kind,canonical);
    if(!row)throw reject('RESOURCE_NOT_FOUND',404);
    return row.original;
  }
  function productIds(product) {
    return {...product,product_id:encodeId('prod',product.product_id),variants:product.variants.map(v=>({...v,variant_id:encodeId('var',v.variant_id)}))};
  }
  function resultIds(operation,result) {
    if(operation==='search_products')return {...result,results:result.results.map(productIds)};
    if(operation==='get_product')return productIds(result);
    return {...result,cart_id:encodeId('cart',result.cart_id),line_items:result.line_items.map(item=>({...item,line_id:encodeId('line',item.line_id),product_id:encodeId('prod',item.product_id),...(item.variant_id?{variant_id:encodeId('var',item.variant_id)}:{})}))};
  }
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
    if(binding.operation==='create_cart' && input.line_items?.length)throw reject('INVALID_INPUT',400);
    for(const [name,kind] of Object.entries({product_id:'prod',variant_id:'var',cart_id:'cart',line_id:'line'}))
      if(values[name]!==undefined)values[name]=decodeId(kind,values[name]);
    if(binding.operation==='add_to_cart') {
      if(!values.variant_id)throw reject('INVALID_INPUT',400);
      const productRoute=plan.bindings.find(b=>b.operation==='get_product');
      if(!productRoute || !values.product_id)throw reject('INVALID_INPUT',400);
      let productPath=productRoute.route.path;
      for(const [name,canonical] of Object.entries(productRoute.path_params))productPath=productPath.replace(':'+name,encodeURIComponent(values[canonical]));
      const response=await fetch(new URL(productPath,base),{redirect:'error',signal:AbortSignal.timeout(15000)});
      if(!response.ok || !response.headers.get('content-type')?.includes('application/json'))throw reject('RESOURCE_NOT_FOUND',404);
      const product=normalizeProduct(await response.json());
      if(product.product_id!==values.product_id || !product.variants.some(v=>v.variant_id===values.variant_id))throw reject('INVALID_INPUT',400);
    }
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
    if(ctx.expectedRevision!==undefined && !binding.revision_field) throw reject('INVALID_INPUT',400);
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
    return resultIds(binding.operation,normalizeResult(binding.operation,await response.json()));
  }]));
  return {adapters,close:()=>db.close()};
}
