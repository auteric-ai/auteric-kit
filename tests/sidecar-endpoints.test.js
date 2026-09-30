import {test} from 'node:test';
import assert from 'node:assert/strict';
import {sidecarEndpoints} from '../src/sidecar-connect.js';
test('hosted Control cannot register merchant loopback',()=>{
 assert.throws(()=>sidecarEndpoints('https://control.auteric.com'),/sidecar-public-url/);
 assert.throws(()=>sidecarEndpoints('https://control.auteric.com',{'sidecar-public-url':'http://127.0.0.1:8089'}),/sidecar-public-url/);
 const result=sidecarEndpoints('https://control.auteric.com',{'sidecar-public-url':'https://agent.shop.example'});
 assert.deepEqual(result,{local:'http://127.0.0.1:8089',endpoint:'https://agent.shop.example'});
});
test('same-host diagnostic retains explicit loopback and refuses malformed routing',()=>{
 assert.equal(sidecarEndpoints('http://127.0.0.1:8100').endpoint,'http://127.0.0.1:8089');
 for(const endpoint of ['https://user:secret@shop.example','https://shop.example/bridge','https://shop.example/?secret=x'])
  assert.throws(()=>sidecarEndpoints('https://control.auteric.com',{'sidecar-public-url':endpoint}));
 assert.throws(()=>sidecarEndpoints('https://control.auteric.com',{'sidecar-url':'https://shop.example','sidecar-public-url':'https://agent.shop.example'}));
});
