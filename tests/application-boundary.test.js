import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { inventoryRepo } from '../src/inventory/index.js';
import { bindRepo } from '../src/binding/index.js';
import { stripTypeScriptTypes } from 'node:module';
import { pathToFileURL } from 'node:url';

function fixture(t, label, receiver, factory, wrapper) {
  const root = mkdtempSync(join(tmpdir(), `auteric-boundary-${label}-`));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, 'server'));
  writeFileSync(join(root, 'package.json'), JSON.stringify({ type: 'module', dependencies: { express: '4.21.2' } }));
  writeFileSync(join(root, 'server/services.js'), `export function ${factory}(dependencies) { return { searchProducts(state, request, options) {}, getProduct(state, request, id) {}, createCart(state, owner) {}, getCart(state, owner, id) {}, addItem(state, owner, id, input) {}, updateItem(state, owner, id, line, input) {} }; }`);
  const source = `import express from 'express';
import { ${factory} } from './services.js';
export function createApp(db) {
 const app = express();
 const ${receiver} = ${factory}({db});
 ${wrapper}('get', '/api/products', {}, ctx => ${receiver}.searchProducts(ctx.state, ctx.req, ctx.req.query));
 ${wrapper}('get', '/api/products/:id', {}, ctx => ${receiver}.getProduct(ctx.state, ctx.req, ctx.req.params.id));
 ${wrapper}('get', '/api/admin/products', {auth:'admin'}, ctx => ${receiver}.getProduct(ctx.state, ctx.req, ctx.req.params.id));
 ${wrapper}('get', '/api/admin/orders', {auth:'admin'}, ctx => []);
 ${wrapper}('post', '/api/carts', {auth:'session', idempotent:true}, ctx => ${receiver}.createCart(ctx.state, ctx.owner));
 ${wrapper}('get', '/api/carts', {auth:'session'}, ctx => ${receiver}.getCart(ctx.state, ctx.owner, undefined));
 ${wrapper}('get', '/api/carts/:id', {auth:'session'}, ctx => ${receiver}.getCart(ctx.state, ctx.owner, ctx.req.params.id));
 ${wrapper}('post', '/api/carts/:id/items', {auth:'session', idempotent:true}, ctx => ${receiver}.addItem(ctx.state, ctx.owner, ctx.req.params.id, ctx.req.body));
 ${wrapper}('patch', '/api/carts/:id/items/:lineId', {auth:'session'}, ctx => ${receiver}.updateItem(ctx.state, ctx.owner, ctx.req.params.id, ctx.req.params.lineId, ctx.req.body));
 ${wrapper}('put', '/api/carts/:id/items', {auth:'session'}, ctx => ctx.state);
 ${wrapper}('post', '/api/sandbox/payments/:id/complete', {}, ctx => ctx.state);
 app.get('/products/:id', (req,res) => { ${receiver}.getProduct({},req,req.params.id); res.type('html').send('<html></html>'); });
 // A call outside a registered handler must not be attached to the last route.
 ${receiver}.getProduct({}, {}, 'unrelated');
 return app;
}`;
  writeFileSync(join(root, 'server/app.js'), source);
  return { root, source };
}

for (const names of [['tea', 'catalogService', 'createCatalogService', 'registerRoute'], ['books', 'commerce', 'buildCommerce', 'attachEndpoint']]) {
  test(`factory provenance and route semantics are independent of merchant names: ${names[0]}`, async t => {
    const { root } = fixture(t, ...names);
    const report = await inventoryRepo(root);
    const candidate = operation => report.candidates.find(item => item.operation === operation);
    assert.equal(candidate('search_products').evidence.business_symbol.factory.name, names[2]);
    assert.equal(candidate('search_products').evidence.entrypoints[0].path, '/api/products');
    assert.equal(candidate('get_product').evidence.entrypoints[0].path, '/api/products/:id');
    assert.equal(report.candidates.filter(item => item.operation === 'get_product').length, 1, 'HTML product page is not a business API');
    assert.equal(candidate('get_cart').evidence.entrypoints[0].path, '/api/carts/:id');
    assert.equal(candidate('update_cart_item').evidence.entrypoints[0].path, '/api/carts/:id/items/:lineId');
    assert.equal(candidate('replace_cart_items').evidence.entrypoints[0].path, '/api/carts/:id/items');
    assert.ok(!candidate('complete_checkout'));
    assert.ok(!candidate('get_order'));
    for (const operation of ['search_products', 'get_product', 'create_cart', 'get_cart', 'add_to_cart']) {
      assert.equal(candidate(operation).strategy, 'requires_merchant_decision');
      assert.match(candidate(operation).reasons.join(' '), /application boundary required/);
    }
    const node = report.graph.nodes.find(item => item.roles.includes('backend'));
    assert.ok(node.routes.some(route => route.path === '/api/admin/products'), 'admin stays in full inventory');
    const cart = node.routes.find(route => route.path === '/api/carts' && route.method === 'POST');
    assert.equal(cart.auth[0].type, 'session');
    assert.equal(cart.application_boundary.idempotent, true);
    const sandbox = node.routes.find(route => route.path.includes('/sandbox/'));
    assert.equal(sandbox.serviceCalls.length, 0, 'unrelated later call cannot contaminate route evidence');
  });
}

test('Sidecar compatibility failure leaves diagnostics without executable scaffolds or merchant edits', async t => {
  const { root, source } = fixture(t, 'clean', 'storeService', 'createStoreService', 'mapRoute');
  await assert.rejects(bindRepo(root, {
    approveCandidates: true, requireMerchantSelection: true,
    requiredServiceOperations: ['search_products', 'get_product', 'create_cart', 'get_cart', 'add_to_cart'],
  }), error => error.code === 'application_integration_required' && /No executable adapters were generated/.test(error.message));
  assert.equal(readFileSync(join(root, 'server/app.js'), 'utf8'), source);
  assert.equal(existsSync(join(root, 'server/auteric')), false);
  assert.equal(existsSync(join(root, '.auteric/installation.json')), false);
  const diagnostics = JSON.parse(readFileSync(join(root, '.auteric/adapter-candidates.json')));
  assert.ok(diagnostics.decisions.some(item => item.operation === 'search_products' && /application boundary/.test(item.decision)));
});

test('collection reads are search candidates even without a service symbol', async t => {
  const { root } = fixture(t, 'inline', 'service', 'makeService', 'registerRoute');
  writeFileSync(join(root, 'server/app.js'), `import express from 'express'; const app = express(); app.get('/api/products', (req,res) => res.json([])); app.get('/api/products/:id', (req,res) => res.json({}));`);
  const report = await inventoryRepo(root);
  assert.equal(report.candidates.find(item => item.operation === 'search_products').evidence.entrypoints[0].path, '/api/products');
  assert.equal(report.candidates.find(item => item.operation === 'get_product').evidence.entrypoints[0].path, '/api/products/:id');
});

test('positional imported service is rejected before Connect generates an object-call scaffold', async t => {
  const { root } = fixture(t, 'positional', 'service', 'makeService', 'registerRoute');
  writeFileSync(join(root, 'server/services.js'), `export class CatalogService { static searchProducts(state, request, options) { return []; } }`);
  writeFileSync(join(root, 'server/app.js'), `import express from 'express'; import {CatalogService} from './services.js'; const app=express(); app.get('/api/products', (req,res) => CatalogService.searchProducts({},req,{}));`);
  await assert.rejects(bindRepo(root, { approveCandidates:true, requireMerchantSelection:true, requiredServiceOperations:['search_products'] }), /not positional or opaque arguments/);
  assert.equal(existsSync(join(root, 'server/auteric')), false);
});

for (const searchField of ['q', 'query']) test(`generated catalog adapter preserves ${searchField} and real no-match behavior`, async t => {
  const { root } = fixture(t, 'query', 'service', 'makeService', 'registerRoute');
  writeFileSync(join(root, 'server/services.js'), `export class CatalogService { static searchProducts({${searchField}='',limit=20}={}) { return {results:[{product_id:'tea',title:'Tea'},{product_id:'book',title:'Book'}].filter(p=>p.title.toLowerCase().includes(${searchField}.toLowerCase())).slice(0,limit)}; } }`);
  writeFileSync(join(root, 'server/app.js'), `import express from 'express'; import {CatalogService} from './services.js'; const app=express(); app.get('/api/products', (req,res) => CatalogService.searchProducts({q:req.query.query}));`);
  const result = await bindRepo(root, { approveCandidates:true, requireMerchantSelection:true, requiredServiceOperations:['search_products'] });
  const file = join(root, result.plan.bindings[0].adapter_file);
  const compiled = file.replace(/\.ts$/, '.mjs');
  writeFileSync(compiled, stripTypeScriptTypes(readFileSync(file, 'utf8'), {mode:'strip'}));
  const {catalogAdapters} = await import(pathToFileURL(compiled));
  const adapter = catalogAdapters().search_products;
  assert.deepEqual((await adapter({principal:'verified'}, {q:'Tea',limit:10})).results.map(p=>p.product_id), ['tea']);
  assert.deepEqual((await adapter({principal:'verified'}, {q:'absent',limit:10})).results, []);
});
