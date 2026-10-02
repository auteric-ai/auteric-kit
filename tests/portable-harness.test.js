import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {bundledHarness} from '../src/connect/harness.js';

test('portable Gateway starts with pinned SDK revision validation and no monorepo',()=>{
  const directory=mkdtempSync(join(tmpdir(),'connect-harness-test-'));
  try {
    const context=bundledHarness();
    const result=execFileSync(context.python,['-c',`
import json,sys
from pathlib import Path
from services.commerce.app import create_app
from services.commerce.transports.native_http import contracts
from auteric_edge.models import INPUTS
app=create_app(str(Path(sys.argv[1])/'gateway.sqlite'),public_url='http://127.0.0.1:8100',development=True)
payload=dict(cart_id='cart_test',product_id='prod_test',quantity=1,expected_revision=7)
assert INPUTS['add_to_cart'].model_validate(payload).model_dump()['expected_revision']==7
for revision in [-1,'7',True]:
 try: INPUTS['add_to_cart'].model_validate(dict(payload,expected_revision=revision))
 except ValueError: pass
 else: raise AssertionError('invalid revision accepted')
assert callable(app.state.sidecar_config)
print(json.dumps({'status':'passed','registry_digest':contracts().REGISTRY_DIGEST}))
`,directory],{encoding:'utf8',env:{...process.env,PYTHONPATH:context.source},cwd:directory});
    assert.equal(JSON.parse(result.trim().split('\n').at(-1)).status,'passed');
    assert.ok(!context.source.includes('/packages/merchant-runtime'));
  } finally {rmSync(directory,{recursive:true,force:true});}
});
