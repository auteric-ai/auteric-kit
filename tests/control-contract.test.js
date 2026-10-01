import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateRegistration} from '../src/connect/control-contract.js';
import {readFileSync,writeFileSync,mkdtempSync,mkdirSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {resolveRuntimeSource} from '../src/connect/local-acceptance.js';

const digest='sha256:'+'a'.repeat(64);
const native={environment:'production',endpoint:'https://merchant.example',transport:'native_http',
  native_runtime:{binding_digest:digest,trusted_proxy_prefix:null,max_body_bytes:1048576},
  protocol_version:'1',sdk_version:'merchant-sidecar/0.1.0',release_id:'qualified',
  operations:[{operation:'get_product',contract_digest:digest,binding_digest:digest}]};
test('native, legacy Service-First and managed module registration match qualified Control',()=>{
  assert.equal(validateRegistration(native),native);
  const managed={...native,sidecar:{integration_version:'0.1.0',storage_mode:'durable',storage_backend:'postgres',agent_ingress:true,
    allowed_operations:['get_product'],profiles:{get_product:{operation:'get_product',enabled:false,mapping_fingerprint:digest,
      target:{base_url:'http://127.0.0.1:3101',path:'/invoke/get_product',method:'POST',auth_scheme:'bearer',
        credential_ref:'env:AUTERIC_BRIDGE_TOKEN',credential_header:'authorization'}}}}};
  assert.equal(validateRegistration(managed),managed);
  assert.throws(()=>validateRegistration({...native,native_runtime:{...native.native_runtime,runtime_kind:'sidecar'}}),/runtime_api_incompatible/);
  assert.throws(()=>validateRegistration({...managed,sidecar:{...managed.sidecar,unexpected:true}}),/runtime_api_incompatible/);
  for(const path of ['../src/cli.js','../src/connect/prepare.js'])
    assert.doesNotMatch(readFileSync(new URL(path,import.meta.url),'utf8'),/runtime_kind\s*:/);
});
test('packaged Connect locates a sibling source and rejects incomplete explicit sources',()=>{
  const root=mkdtempSync(join(tmpdir(),'connect-source-'));
  try {
    const source=join(root,'platform'),merchant=join(root,'merchant');mkdirSync(merchant);
    mkdirSync(join(source,'services/commerce'),{recursive:true});mkdirSync(join(source,'packages/commerce-contracts/vectors/conformance'),{recursive:true});
    writeFileSync(join(source,'services/commerce/app.py'),'# source marker');
    // An explicit source never silently falls back to another baseline.
    assert.throws(()=>resolveRuntimeSource(merchant,{'runtime-source':merchant}),/runtime_source_required/);
    assert.equal(resolveRuntimeSource(merchant,{'runtime-source':source}),source);
  } finally {rmSync(root,{recursive:true,force:true});}
});
