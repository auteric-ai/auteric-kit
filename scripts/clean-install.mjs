// Installs an unpublished tarball in a disposable consumer. This checks package
// layout and dependencies, not npm publication or anonymous image pull.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, realpathSync, mkdirSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { fixtureMapping } from '../runtime/merchant-runtime/tests/merchant-fixture.js';

const kit=fileURLToPath(new URL('../',import.meta.url));
const root=mkdtempSync(join(realpathSync(tmpdir()),'connect-clean-package-'));
try {
  const [pack]=JSON.parse(execFileSync('npm',['pack','--json','--pack-destination',root],{cwd:kit,encoding:'utf8'}));
  const consumer=join(root,'consumer'),merchant=join(root,'merchant');
  mkdirSync(consumer);mkdirSync(merchant);
  writeFileSync(join(consumer,'package.json'),'{"private":true,"type":"module"}\n');
  execFileSync('npm',['install','--ignore-scripts','--no-audit','--no-fund',join(root,pack.filename)],{cwd:consumer,stdio:'pipe'});
  const mapping=fixtureMapping('b'),{operations,...metadata}=mapping;delete metadata.schema;
  const paths={[mapping.session.path]:{post:{operationId:'issue_visitor'}}};
  for(const op of operations) {
    const {operation,target,...fields}=op;paths[target.path]||={};paths[target.path][target.method.toLowerCase()]={operationId:operation,'x-auteric':fields};
  }
  writeFileSync(join(merchant,'package.json'),'{"private":true,"dependencies":{"express":"4"}}');
  writeFileSync(join(merchant,'openapi.json'),JSON.stringify({openapi:'3.0.3',info:{title:'Independent merchant',version:'1'},'x-auteric':metadata,paths}));
  const cli=join(consumer,'node_modules/@auteric/cli/bin/auteric.js');
  const output=execFileSync(process.execPath,[cli,'connect','--domain','independent.example','--backend','.','--frontend','.','--mapping','openapi.json','--dry-run'],{cwd:merchant,encoding:'utf8'});
  assert.match(output,/Dry run/);assert.equal(existsSync(join(merchant,'.auteric')),false);assert.equal(existsSync(join(merchant,'auteric')),false);
  console.log(JSON.stringify({scope:'unpublished_local_tarball',status:'passed',version:pack.version,clean_dependency_install:true,independent_mapping_preview:true,merchant_files_written:0,public_release_qualified:false}));
} finally { rmSync(root,{recursive:true,force:true}); }
