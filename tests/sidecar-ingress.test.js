import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {waitForSidecarIngress} from '../src/single-command.js';

test('a temporary edge failure is resolved using only read-only liveness requests',async t=>{
 let requests=0;
 const server=createServer((req,res)=>{
  assert.equal(req.method,'GET');assert.equal(req.url,'/health/live');requests++;
  if(requests===1){res.writeHead(520);res.end('edge not ready');return;}
  res.setHeader('content-type','application/json');res.end(JSON.stringify({status:'live',merchant_protocol:'1'}));
 });
 await new Promise(done=>server.listen(0,'127.0.0.1',done));t.after(()=>server.close());
 await waitForSidecarIngress('http://127.0.0.1:'+server.address().port+'/api/auteric/v1',{exitCode:null},{timeoutMs:2000});
 assert.equal(requests,2);
});
test('an HTML success page cannot verify Sidecar ingress',async t=>{
 const server=createServer((req,res)=>{res.setHeader('content-type','text/html');res.end('<html>ok</html>');});
 await new Promise(done=>server.listen(0,'127.0.0.1',done));t.after(()=>server.close());
 await assert.rejects(waitForSidecarIngress('http://127.0.0.1:'+server.address().port,{exitCode:null},{timeoutMs:100}),/no connection-test action was submitted/);
});
