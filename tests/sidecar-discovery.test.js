import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,realpathSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {installDiscoveryRoute,restoreDiscoveryRoute} from '../src/sidecar-discovery.js';
test('discovery route preserves existing merchant source and refuses to overwrite later edits',()=>{
 const root=mkdtempSync(join(realpathSync(tmpdir()),'discovery-route-'));mkdirSync(join(root,'server'));mkdirSync(join(root,'public'));
 const entry=join(root,'server/app.js'),original="import express from 'express';\nconst app=express();\napp.get('/products',(_req,res)=>res.json([]));\n";
 try{writeFileSync(entry,original);assert.equal(installDiscoveryRoute(root,join(root,'public')),true);const installed=readFileSync(entry,'utf8');assert.ok(installed.includes("app.get('/products'"));assert.equal(installDiscoveryRoute(root,join(root,'public')),false);writeFileSync(entry,installed+'// merchant edit\n');assert.throws(()=>restoreDiscoveryRoute(root),/Merchant edits/);assert.ok(readFileSync(entry,'utf8').includes('// merchant edit'));writeFileSync(entry,installed);assert.equal(restoreDiscoveryRoute(root),true);assert.equal(readFileSync(entry,'utf8'),original);}finally{rmSync(root,{recursive:true,force:true});}
});
