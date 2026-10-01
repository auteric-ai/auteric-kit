import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import Ajv from 'ajv/dist/2020.js';
import { REGISTRY_DIGEST, OPERATIONS } from './contracts.generated.js';

export const MVP_OPERATIONS = Object.freeze(['search_products', 'get_product', 'create_cart', 'get_cart', 'add_to_cart']);
const ajv = new Ajv({ allErrors: true, strict: true, allowUnionTypes: true });
for (const name of ['bridge-mapping', 'module-binding', 'connection', 'deployment']) {
  ajv.addSchema(JSON.parse(readFileSync(new URL(`../schemas/${name}.schema.json`, import.meta.url), 'utf8')));
}
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}
export function digest(value) { return 'sha256:' + createHash('sha256').update(canonical(value)).digest('hex'); }
export function validateDocument(name, value) {
  if (Buffer.byteLength(JSON.stringify(value)) > 65536) throw Error('configuration exceeds 64 KiB');
  const validate = ajv.getSchema(`${name}.schema.json`);
  if (!validate || !validate(value)) throw Error(`invalid ${name}: ${ajv.errorsText(validate?.errors)}`);
  const inspect = value => {
    if (!value || typeof value !== 'object') return;
    for (const [key, item] of Object.entries(value)) {
      if (['__proto__', 'prototype', 'constructor'].includes(key)) throw Error('unsafe configuration key');
      inspect(item);
    }
  };
  inspect(value);
  return value;
}
export function relativePath(value) {
  if (!value.startsWith('/') || value.startsWith('//') || /[\\%?#]/.test(value)
      || value.split('/').some(s => ['.', '..'].includes(s))) throw Error('unsafe relative merchant route');
  return value;
}
export function backendOrigin(value, { privateHosts = [] } = {}) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw Error('backend must be a credential-free HTTP(S) origin');
  if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]', ...privateHosts].includes(url.hostname)) throw Error('HTTP backend requires loopback or an explicitly approved private host');
  return url.origin;
}
export function pointer(value, path, optional = false) {
  if (path === '') return value;
  let result = value;
  for (const part of path.slice(1).split('/').map(s => s.replace(/~1/g, '/').replace(/~0/g, '~'))) {
    if (['__proto__', 'constructor', 'prototype'].includes(part)) throw Error('unsafe field selector');
    if (result === null || typeof result !== 'object' || !Object.hasOwn(result, part)) {
      if (optional) return undefined;
      throw Error(`authoritative field missing: ${path}`);
    }
    result = result[part];
  }
  return result;
}
export function validateMapping(raw) {
  if (raw.schema === 'auteric-module-binding/v1') {
    validateDocument('module-binding',raw);
    if(raw.registry_digest!==REGISTRY_DIGEST)throw Error('registry digest mismatch');
    const seen=new Set();
    for(const op of raw.operations) {
      if(seen.has(op.operation) || !OPERATIONS[op.operation] || op.side_effect!==OPERATIONS[op.operation].sideEffect)
        throw Error('module binding operations/effects differ from registry');
      seen.add(op.operation);
    }
    return raw;
  }
  validateDocument('bridge-mapping', raw);
  if (raw.registry_digest !== REGISTRY_DIGEST) throw Error('registry digest mismatch');
  relativePath(raw.session.path);
  const seen = new Set();
  for (const op of raw.operations) {
    if (seen.has(op.operation)) throw Error('duplicate operation mapping');
    seen.add(op.operation);
    relativePath(op.target.path);
    if (!Object.hasOwn(raw.profiles, op.output.profile)) throw Error('unknown output profile');
    const placeholders = [...op.target.path.matchAll(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g)].map(m => m[1]).sort();
    if (JSON.stringify(placeholders) !== JSON.stringify(Object.keys(op.input.path).sort())) throw Error('path parameters do not match route');
    const writes = ['create_cart', 'add_to_cart'].includes(op.operation);
    if (op.target.method !== (writes ? 'POST' : 'GET')) throw Error('operation method mismatch');
    if (writes && (!op.idempotency || op.auth !== 'session')) throw Error('writes require merchant session and idempotency support');
    if (['get_cart', 'add_to_cart'].includes(op.operation) && op.auth !== 'session') throw Error('cart ownership requires merchant session');
    if (op.operation === 'add_to_cart' && !op.revision) throw Error('implementation_required: add_to_cart needs atomic expected_revision support on the merchant route');
    const allowed = {
      search_products: ['q', 'category', 'currency', 'cursor', 'limit', 'price_min_minor', 'price_max_minor'],
      get_product: ['product_id'], create_cart: ['currency', 'line_items'], get_cart: ['cart_id'],
      add_to_cart: ['cart_id', 'product_id', 'variant_id', 'quantity'],
    }[op.operation];
    for (const part of Object.values(op.input)) for (const field of Object.values(part)) {
      if (!allowed.includes(field.select.slice(1)) || field.select.slice(1).includes('/')) throw Error('input selector must reference an allowed canonical field');
      if (field.transform === 'id' && !field.kind) throw Error('ID selector needs explicit kind');
    }
  }
  const visitProfile = (name, chain = []) => {
    if (chain.includes(name) || chain.length > 8 || !Object.hasOwn(raw.profiles, name)) throw Error('cyclic or unknown output profile');
    for (const field of Object.values(raw.profiles[name])) {
      if (field.transform === 'money' && (!field.currency || !field.unit)) throw Error('money requires explicit currency selector and unit');
      if (field.transform === 'id' && !field.kind) throw Error('ID selector needs explicit kind');
      if (field.transform === 'enum' && !field.values) throw Error('enum requires explicit values');
      if (['array', 'object'].includes(field.transform)) visitProfile(field.profile, [...chain, name]);
    }
  };
  for (const name of Object.keys(raw.profiles)) visitProfile(name);
  return raw;
}
export function validateConnection(raw, options) {
  validateDocument('connection', raw); validateMapping(raw.mapping);
  if (digest(raw.mapping) !== raw.mapping_digest) throw Error('mapping digest mismatch');
  if (new URL(raw.control_origin).protocol !== 'https:' || new URL(raw.control_origin).origin !== raw.control_origin) throw Error('Control origin requires credential-free HTTPS');
  backendOrigin(raw.backend.origin, options);
  return raw;
}
