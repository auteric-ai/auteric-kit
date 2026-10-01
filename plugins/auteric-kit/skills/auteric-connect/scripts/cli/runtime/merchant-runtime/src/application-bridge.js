import { translationHelpers, BridgeError, fail } from './bridge-helpers.js';
export { BridgeError } from './bridge-helpers.js';
import { validateMapping, backendOrigin, pointer } from './mapping.js';
import { OPERATIONS, CURRENCY_EXPONENTS, DEFAULT_CURRENCY_EXPONENT } from './contracts.generated.js';

export function exactMoney(amount, currency, unit) {
  if (typeof currency !== 'string' || !/^[A-Z]{3}$/.test(currency)) throw Error('authoritative currency missing');
  let minor;
  if (unit === 'minor') {
    if (!Number.isSafeInteger(amount) || amount < 0) throw Error('authoritative minor-unit amount invalid');
    minor = amount;
  } else if (unit === 'decimal') {
    // Decimal strings only. Binary floating-point values are not an exact price source.
    if (typeof amount !== 'string' || !/^\d{1,16}(\.\d{1,8})?$/.test(amount)) throw Error('exact decimal amount string required');
    const exponent = CURRENCY_EXPONENTS[currency] ?? DEFAULT_CURRENCY_EXPONENT;
    const [whole, fraction = ''] = amount.split('.');
    if (fraction.slice(exponent).replace(/0/g, '')) throw Error('amount has fractional minor units');
    const value = BigInt(whole) * 10n ** BigInt(exponent) + BigInt(fraction.slice(0, exponent).padEnd(exponent, '0') || '0');
    if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw Error('money exceeds safe integer');
    minor = Number(value);
  } else throw Error('unknown money unit');
  return { amount_minor: minor, currency };
}

/** This boundary translates HTTP only. Signature/policy/replay remain in the unchanged Sidecar. */
export function applicationAdapters(mapping, { origin, installationId, statePath, privateHosts = [], fetcher = fetch, timeoutMs = 10000, databaseUrl, store } = {}) {
  validateMapping(mapping);
  if (!installationId || (!statePath && !databaseUrl && !store)) throw Error('installation and durable bridge state are required');
  const {base,query,one,encodeId,decodeId,http,session,close} = translationHelpers({origin,installationId,statePath,privateHosts,fetcher,timeoutMs,databaseUrl,store,sessionConfig:mapping.session});
  async function profile(name, value) {
    const output = {};
    for (const [key, spec] of Object.entries(mapping.profiles[name])) {
      const selected = pointer(value, spec.select, spec.optional === true);
      if (selected === undefined) continue;
      let result;
      switch (spec.transform) {
        case 'copy': result = selected; break;
        case 'id': result = await encodeId(spec.kind, selected); break;
        case 'money': result = exactMoney(selected, pointer(value, spec.currency), spec.unit); break;
        case 'enum':
          if (!Object.hasOwn(spec.values, String(selected))) throw Error('unknown authoritative enum');
          result = spec.values[String(selected)]; break;
        case 'array':
          if (!Array.isArray(selected) || selected.length > 250) throw Error('bounded authoritative array required');
          result = await Promise.all(selected.map(item => profile(spec.profile, item))); break;
        case 'object': result = await profile(spec.profile, selected); break;
        case 'page_number':
          if (!Number.isSafeInteger(selected) || selected < 1) throw Error('invalid page number');
          result = String(selected + 1); break;
        case 'zero_discounts':
          if (selected !== 0) throw Error('discount breakdown required');
          result = []; break;
        default: throw Error('unknown transform');
      }
      output[key] = result;
    }
    return output;
  }
  const adapters = Object.fromEntries(mapping.operations.map(op => [op.operation, async (ctx, input) => {
    if (ctx.installationId !== installationId || ctx.operation !== op.operation || !ctx.subject || !ctx.principal || !ctx.actionId || ctx.contractVersion !== '1.0.0') fail('INVALID_INPUT', 400, 'context does not match installation/operation/contract');
    OPERATIONS[op.operation].validateInput(input);
    // No caller-controlled identity, action, revision or path override can enter the translation.
    const accepted=new Set(Object.values(op.input).flatMap(group=>Object.values(group).map(spec=>spec.select.slice(1))));
    for(const key of Object.keys(input)) {
      if(key==='expected_revision' && op.revision)continue;
      if(key==='line_items' && Array.isArray(input[key]) && input[key].length===0)continue;
      if(!accepted.has(key))fail('INVALID_INPUT',400,`unsupported canonical input field: ${key}`);
    }
    const values = { ...input };
    for (const [key, value] of Object.entries(ctx.pathParams || {})) {
      if (!['product_id','cart_id'].includes(key) || (Object.hasOwn(values,key) && values[key] !== value)) fail('INVALID_INPUT', 400);
      values[key] = value;
    }
    if (op.operation === 'create_cart' && input.line_items?.length) fail('INVALID_INPUT', 400, 'merchant atomic initial-line cart creation is implementation_required');
    const convert = async spec => {
      const value = pointer(values, spec.select, true);
      if (value === undefined) return undefined;
      if (spec.transform === 'id') return await decodeId(spec.kind, value);
      if (spec.transform === 'cursor_page') {
        if (typeof value !== 'string' || !/^[1-9]\d{0,8}$/.test(value)) fail('INVALID_INPUT',400,'unsupported cursor');
        return Number(value);
      }
      return value;
    };
    let path = op.target.path;
    for (const [name, spec] of Object.entries(op.input.path)) {
      const value = await convert(spec);
      // Encoded dot/path segments can be normalized by intermediaries; fail before dispatch.
      if (typeof value !== 'string' || !value || /[\\/%?#]/.test(value) || ['.','..'].includes(value)) fail('INVALID_INPUT',400,'unsafe path ID');
      path = path.replace(`{${name}}`, encodeURIComponent(value));
    }
    const url = new URL(path, base), body = {}, headers = {accept:'application/json'};
    for (const [name, spec] of Object.entries(op.input.query)) { const value = await convert(spec); if (value !== undefined) url.searchParams.set(name,String(value)); }
    for (const [name, spec] of Object.entries(op.input.body)) { const value = await convert(spec); if (value !== undefined) body[name] = value; }
    if (op.auth === 'session') headers.cookie = await session(ctx);
    if (op.idempotency) headers[op.idempotency.header] = ctx.actionId;
    const revision = ctx.expectedRevision;
    if (input.expected_revision !== undefined && input.expected_revision !== revision) fail('INVALID_INPUT',400,'revision must match verified context');
    if (revision !== undefined) {
      if (!op.revision || !Number.isSafeInteger(revision) || revision < 0) fail('INVALID_INPUT',400,'unsupported revision');
      if (op.revision.location === 'header') headers[op.revision.field] = String(revision);
      else body[op.revision.field] = revision;
    }
    if(op.operation==='add_to_cart' && values.variant_id !== undefined && !(await one('SELECT 1 FROM bridge_variants WHERE installation=? AND product=? AND variant=?',installationId,values.product_id,values.variant_id)))fail('INVALID_INPUT',400,'variant does not belong to the selected product');
    const options = {method:op.target.method,headers};
    if (op.target.method !== 'GET') {headers['content-type']='application/json';options.body=JSON.stringify(body);}
    const response = await http(url.pathname + url.search, options);
    let byteCount=0;const chunks=[];
    for await(const chunk of response.body){byteCount+=chunk.length;if(byteCount>1048576)throw Error('merchant response exceeds 1 MiB');chunks.push(Buffer.from(chunk));}
    const raw=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const result = await profile(op.output.profile, pointer(raw,op.output.select));
    OPERATIONS[op.operation].validateOutput(result);
    // Schemas validate shape; monetary consistency is checked here without manufacturing values.
    if (result.currency && [result.subtotal,result.discount_total,result.total,...(result.line_items || []).flatMap(i=>[i.unit_price,i.total_price])].some(m=>m.currency!==result.currency)) throw Error('mixed cart currency');
    if (op.operation === 'create_cart' && result.currency !== input.currency) throw Error('merchant cart currency differs from requested currency');
    const products=op.operation==='search_products'?result.results:op.operation==='get_product'?[result]:[];
    for(const product of products)for(const variant of product.variants)await query('INSERT INTO bridge_variants VALUES(?,?,?) ON CONFLICT DO NOTHING',installationId,product.product_id,variant.variant_id);
    if (result.variants && new Set(result.variants.map(v=>v.price.currency)).size !== 1) throw Error('mixed product currency');
    return result;
  }]));
  return {adapters, close};
}
