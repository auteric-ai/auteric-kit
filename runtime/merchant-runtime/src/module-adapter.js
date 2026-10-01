import { readFileSync, realpathSync } from 'node:fs';
import { resolve, relative, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { translationHelpers, fail } from './bridge-helpers.js';
import { exactMoney } from './application-bridge.js';
import { OPERATIONS, REGISTRY_DIGEST } from './contracts.generated.js';

export function containedFile(root, name) {
  const base = realpathSync(root), target = realpathSync(resolve(base, name));
  if (relative(base, target).startsWith('..') || target === base) throw Error('adapter path escapes integration directory');
  return target;
}

export async function loadAdapter(connectionPath) {
  const config = JSON.parse(readFileSync(connectionPath, 'utf8'));
  if (config.schema !== 'auteric-module-connection/v1' || config.registry_digest !== REGISTRY_DIGEST)
    throw Error('adapter contract/registry mismatch');
  const names = config.operations;
  if (!Array.isArray(names) || !names.length || new Set(names).size !== names.length || names.some(op => !Object.hasOwn(OPERATIONS, op)))
    throw Error('adapter operations must be a registry subset');
  const path = containedFile(dirname(connectionPath), config.adapter);
  const adapter = await import(pathToFileURL(path).href + '?sha=' + createHash('sha256').update(readFileSync(path)).digest('hex'));
  if (adapter.schema !== 'auteric-adapter/v1' || typeof adapter.merchant !== 'function' || !adapter.projections)
    throw Error('adapter must export merchant boundary and projections');
  for (const op of names) if (typeof adapter.projections[op] !== 'function') throw Error('missing adapter projection: ' + op);
  if (!Array.isArray(config.writes) || JSON.stringify([...config.writes].sort()) !== JSON.stringify(names.filter(op => OPERATIONS[op].sideEffect !== 'read').sort())
    || JSON.stringify([...(adapter.writes || [])].sort()) !== JSON.stringify([...config.writes].sort()))
    throw Error('adapter write effects differ from registry');
  for(const op of names) if(OPERATIONS[op].sideEffect !== 'read' || ['get_cart','get_checkout','get_order'].includes(op)) {
    if(adapter.auth?.[op] !== 'session' || !adapter.session?.path || !adapter.session?.cookie_name)
      throw Error('owned operations require an authoritative merchant session');
  }
  const fingerprint='sha256:'+createHash('sha256').update(readFileSync(path)).update(readFileSync(connectionPath)).digest('hex');
  return { config, adapter, path, fingerprint };
}

const ID_KIND = { product_id: 'product', variant_id: 'variant', cart_id: 'cart', line_id: 'line',
  checkout_id: 'checkout', order_id: 'order', shipping_option_id: 'shipping_option' };
async function identifiers(value, translate) {
  if (Array.isArray(value)) return Promise.all(value.map(item => identifiers(item, translate)));
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(await Promise.all(Object.entries(value).map(async ([key, item]) =>
    [key, ID_KIND[key] && item !== undefined ? await translate(ID_KIND[key], item) : await identifiers(item, translate)])));
}
export function enumValue(value, values) {
  if (!Object.hasOwn(values, value)) throw Error('unknown authoritative enum');
  return values[value];
}

// The dispatcher stays unchanged. This module supplies its adapter map.
// Application callbacks run in the merchant process; projections/helpers run here.
export async function moduleAdapters(connectionPath, options) {
  const { config, adapter, fingerprint } = await loadAdapter(connectionPath);
  if(options.bindingDigest && options.bindingDigest!==fingerprint)throw Error('adapter differs from registered binding');
  const t = translationHelpers({ ...options, sessionConfig: adapter.session, errorMap: adapter.errorMap });
  const h = { money: (amount, currency) => exactMoney(amount, currency, 'minor'), enum: enumValue };
  async function call(ctx, input) {
    const op = OPERATIONS[ctx.operation], write = op.sideEffect !== 'read';
    const headers = { accept: 'application/json', authorization: 'Bearer ' + options.applicationToken };
    if (adapter.auth?.[ctx.operation] === 'session') headers.cookie = await t.session(ctx);
    if (write) headers['idempotency-key'] = ctx.actionId;
    const url = new URL('/api/auteric/private/' + ctx.operation, t.base);
    const request = { method: write ? 'POST' : 'GET', headers };
    if (write) { headers['content-type'] = 'application/json'; request.body = JSON.stringify(input); }
    else url.searchParams.set('input', JSON.stringify(input));
    return (await t.http(url.href, request)).json();
  }
  async function page(ctx, input) {
    const signature = createHash('sha256').update(JSON.stringify([options.installationId,
      Object.fromEntries(Object.entries(input).filter(([key]) => key !== 'cursor').sort())])).digest('hex');
    let offset = 0;
    if (input.cursor) {
      try {
        const parsed = JSON.parse(Buffer.from(input.cursor, 'base64url').toString());
        if (parsed.signature !== signature || !Number.isSafeInteger(parsed.offset) || parsed.offset < 0) throw Error();
        offset = parsed.offset;
      } catch { fail('INVALID_INPUT', 400, 'cursor does not match this search'); }
    }
    const results = [], limit = input.limit ?? 20;
    let count = 0, total = Infinity;
    for (let number = 1; count < total; number++) {
      if (number > 100) fail('UPSTREAM_ERROR', 502, 'merchant pagination budget exceeded');
      const raw = await call(ctx, { ...input, page: number, limit: 50 });
      const p = adapter.pagination[ctx.operation](raw);
      if (!Array.isArray(p.items) || !Number.isSafeInteger(p.total) || p.total < 0 || (!p.items.length && count < p.total))
        throw Error('invalid merchant page');
      total = p.total; count += p.items.length;
      for (const item of p.items) {
        const product = adapter.projections[ctx.operation](item, h);
        const matches = product.variants.some(v => (!input.currency || v.price.currency === input.currency)
          && (input.price_min_minor === undefined || v.price.amount_minor >= input.price_min_minor)
          && (input.price_max_minor === undefined || v.price.amount_minor <= input.price_max_minor));
        if (matches) results.push(product);
      }
      if (results.length > offset + limit) break;
    }
    const more = results.length > offset + limit;
    return { results: results.slice(offset, offset + limit), page: { has_more: more,
      ...(more ? { next_cursor: Buffer.from(JSON.stringify({signature, offset: offset + limit})).toString('base64url') } : {}) } };
  }
  const adapters = Object.fromEntries(config.operations.map(operation => [operation, async (ctx, input) => {
    if (ctx.installationId !== options.installationId || ctx.operation !== operation || !ctx.subject || !ctx.principal
      || !/^action_[A-Za-z0-9]{8,64}$/.test(ctx.actionId) || ctx.contractVersion !== OPERATIONS[operation].contractVersion)
      fail('INVALID_INPUT', 400, 'verified context mismatch');
    OPERATIONS[operation].validateInput(input);
    const expected = [...OPERATIONS[operation].path.matchAll(/\{([a-z_]+)\}/g)].map(match => match[1]);
    if (Object.keys(ctx.pathParams || {}).some(key => !expected.includes(key)) || expected.some(key => !ctx.pathParams?.[key]))
      fail('INVALID_INPUT', 400, 'path context mismatch');
    if (input.expected_revision !== undefined && input.expected_revision !== ctx.expectedRevision)
      fail('INVALID_INPUT', 400, 'revision differs from signed context');
    const native = await identifiers({ ...input, ...ctx.pathParams,
      ...(ctx.expectedRevision === undefined ? {} : { expected_revision: ctx.expectedRevision }) }, t.decodeId);
    const raw = adapter.pagination?.[operation] ? await page(ctx, native) : await call(ctx, native);
    try {
      const projected = adapter.pagination?.[operation] ? raw : adapter.projections[operation](raw, h);
      const output = await identifiers(projected, t.encodeId);
      OPERATIONS[operation].validateOutput(output);
      return output;
    } catch {
      fail(OPERATIONS[operation].sideEffect !== 'read' ? 'EXECUTION_UNCERTAIN' : 'UPSTREAM_ERROR',502,'merchant output cannot be confirmed');
    }
  }]));
  return { adapters, helpers: t, close: t.close };
}

// Enrollment contains a digest and registry subset, never executable merchant code.
export async function moduleBinding(connectionPath) {
  const {config,fingerprint}=await loadAdapter(connectionPath);
  return {schema:'auteric-module-binding/v1',registry_digest:REGISTRY_DIGEST,adapter_fingerprint:fingerprint,
    operations:config.operations.map(operation=>({operation,side_effect:OPERATIONS[operation].sideEffect}))};
}
