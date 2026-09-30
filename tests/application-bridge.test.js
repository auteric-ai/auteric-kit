import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {applicationAdapters,normalizeProduct} from '../src/application-bridge.js';
import {applicationPlan} from '../src/application-plan.js';
import {createServer} from 'node:http';

test('application route mapping follows renamed factory, files and route wrapper',async()=>{
 const root=mkdtempSync(join(tmpdir(),'application-mapping-'));
 try {
  mkdirSync(join(root,'api'));
  writeFileSync(join(root,'package.json'),JSON.stringify({type:'module',dependencies:{express:'4'}}));
  writeFileSync(join(root,'api/business.js'),`export function makeItems(deps){return {lookupItems(state,request,{query,limit}){return deps.search(state,query,limit)}}}`);
  writeFileSync(join(root,'api/http.js'),`import express from 'express';import {makeItems} from './business.js';const service=makeItems({});const app=express();function endpoint(method,path,options,handler){app[method](path,handler)}endpoint('get','/shop/products',{},ctx=>service.lookupItems(ctx.state,ctx.req,{query:ctx.req.query.q,limit:ctx.req.query.limit}));endpoint('get','/shop/admin/products',{},ctx=>service.lookupItems(ctx.state,ctx.req,{}));`);
  const plan=await applicationPlan(root);
  assert.deepEqual(plan.bindings.map(b=>b.operation),['search_products']);
  assert.equal(plan.bindings[0].route.path,'/shop/products');
  assert.equal(plan.bindings[0].fields.q,'q');
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('normalization rejects unknown prices and availability instead of inventing data',()=>{
 assert.throws(()=>normalizeProduct({id:'x',name:'X',variants:[{id:'v',price:10,currency:'USD',availability:'in_stock'}]}),/minor|lacks/);
 assert.throws(()=>normalizeProduct({id:'x',name:'X',variants:[{id:'v',priceCents:100,currency:'USD',availability:'unknown'}]}),/unknown/);
});

test('bridge keeps operation cookies in one durable buyer session and separates buyers',async()=>{
 const root=mkdtempSync(join(tmpdir(),'application-session-'));let issuances=0;const carts=new Map();
 const server=createServer(async(req,res)=>{
  const body=JSON.parse(await new Promise(done=>{let s='';req.on('data',c=>s+=c);req.on('end',()=>done(s||'{}'));}));
  res.setHeader('content-type','application/json');
  if(req.url==='/guest'){issuances++;res.setHeader('set-cookie',`buyer=${issuances}; HttpOnly; Path=/`);res.end('{}');return;}
  const owner=req.headers.cookie;
  if(req.method==='POST'){const replay=[...carts.values()].find(c=>c.key===req.headers['idempotency-key']&&c.owner===owner);if(replay){res.end(JSON.stringify(replay.value));return;}const value={id:String(carts.size+1),revision:0,status:'active',currency:'USD',items:[],subtotalCents:0,discountCents:0,totalCents:0};carts.set(value.id,{owner,key:req.headers['idempotency-key'],value});res.end(JSON.stringify(value));return;}
  const cart=carts.get(req.url.slice(1));if(!cart||cart.owner!==owner){res.statusCode=404;res.end('{}');return;}res.end(JSON.stringify(cart.value));
 });
 await new Promise(done=>server.listen(0,'127.0.0.1',done));
 const options={origin:`http://127.0.0.1:${server.address().port}`,statePath:join(root,'sessions.sqlite'),reject:code=>new Error(code)};
 const plan={session:{method:'POST',path:'/guest'},bindings:[{operation:'create_cart',route:{method:'POST',path:'/'},auth:'session',idempotent:true,fields:{}},{operation:'get_cart',route:{method:'GET',path:'/:id'},auth:'session',fields:{},path_params:{id:'cart_id'}}]};
 let bridge=applicationAdapters(plan,options);
 try{
  const ctx={installationId:'installation',principal:'first',subject:'pairwise-first',actionId:'once'};
  const cart=await bridge.adapters.create_cart(ctx,{});
  assert.deepEqual(await bridge.adapters.create_cart(ctx,{}),cart);assert.equal(carts.size,1);
  bridge.close();bridge=applicationAdapters(plan,options);
  assert.equal((await bridge.adapters.get_cart({...ctx,actionId:'read'},{cart_id:cart.cart_id})).cart_id,cart.cart_id);
  await assert.rejects(()=>bridge.adapters.get_cart({...ctx,principal:'second',subject:'pairwise-second',actionId:'foreign'},{cart_id:cart.cart_id}),/RESOURCE_NOT_FOUND/);
  assert.equal(issuances,2);
 }finally{bridge.close();await new Promise(done=>server.close(done));rmSync(root,{recursive:true,force:true});}
});
