import { cpSync, readFileSync, mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
const kit=fileURLToPath(new URL('../',import.meta.url));
const source=fileURLToPath(new URL('../../../packages/merchant-runtime/',import.meta.url));
const target=join(kit,'runtime/merchant-runtime');
const paths=['package.json','src','schemas','python','Dockerfile.local','package-lock.json','release-manifest.json','tests/merchant-fixture.js'];
if(process.argv.includes('--check')) {
  const compare=(s,t)=>{for(const entry of readdirSync(s,{withFileTypes:true}))entry.isDirectory()?compare(join(s,entry.name),join(t,entry.name)):
    (readFileSync(join(s,entry.name),'utf8')===readFileSync(join(t,entry.name),'utf8')||(()=>{throw Error('shared runtime bundle drift: '+entry.name);})());};
  if(readFileSync(join(source,'tests/merchant-fixture.js'),'utf8')!==readFileSync(join(target,'tests/merchant-fixture.js'),'utf8'))throw Error('shared merchant fixture drift');
  compare(join(source,'src'),join(target,'src'));compare(join(source,'python'),join(target,'python'));compare(join(source,'schemas'),join(target,'schemas'));
  for(const file of ['package.json','release-manifest.json'])if(readFileSync(join(source,file),'utf8')!==readFileSync(join(target,file),'utf8'))throw Error('bundle drift: '+file);
}else{
  mkdirSync(join(target,'tests'),{recursive:true});for(const path of paths)cpSync(join(source,path),join(target,path),{recursive:true});
  cpSync(join(source,'schemas'),join(kit,'schemas'),{recursive:true});cpSync(join(source,'release-manifest.json'),join(kit,'release-manifest.json'));
}
