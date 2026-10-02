import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ownerSession} from '../src/connect/auth.js';
test('login returns immediately, resumes PKCE and verifies owner approval with Control',async()=>{
 const dir=mkdtempSync(join(realpathSync(tmpdir()),'auteric-auth-')),prior=process.env.AUTERIC_SESSION_DIRECTORY;
 process.env.AUTERIC_SESSION_DIRECTORY=dir;let opens=0,starts=0,polls=0,approved=false;
 const deps={openBrowser:()=>opens++,request:async(base,path,options)=>{
  if(path.endsWith('/start')) {starts++;return {authorization_url:base+'/cli/authorize?id=one',request_id:'one',expires_at:Date.now()/1000+300};}
  if(path.endsWith('/poll')) {polls++;assert.ok(options.body.verifier);return approved?{status:'authorized',access_token:'owner',expires_in:3600}:{status:'pending'};}
  return {};
 }};
 try {
  const one=await ownerSession(dir,'https://control.example','shop.example',{},deps);
  assert.equal(one.status,'authentication_pending');assert.equal(polls,0);
  assert.equal((await ownerSession(dir,'https://control.example','shop.example',{},deps)).authorized,false);
  approved=true;assert.equal((await ownerSession(dir,'https://control.example','shop.example',{},deps)).authorized,true);
  assert.equal(starts,1);assert.equal(opens,1);
 }finally{if(prior===undefined)delete process.env.AUTERIC_SESSION_DIRECTORY;else process.env.AUTERIC_SESSION_DIRECTORY=prior;rmSync(dir,{recursive:true,force:true});}
});
