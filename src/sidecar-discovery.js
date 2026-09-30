import {existsSync,readFileSync,writeFileSync} from 'node:fs';
import {join,relative,resolve,dirname} from 'node:path';
import {walkRepo} from './inventory/walk.js';
import {createHash} from 'node:crypto';
import {atomicJSON,readJSON} from './workflow.js';
const hash=s=>createHash('sha256').update(s).digest('hex');
// Add only a profile-serving route. Existing commerce routes/services are untouched.
export function installDiscoveryRoute(backend,frontend){
 const record=join(backend,'.auteric','discovery-route.json');
 const candidates=walkRepo(backend).files.filter(f=>/\.(m?js)$/.test(f.path)&&/\bconst\s+\w+\s*=\s*express\(\);/.test(f.content));
 if(candidates.length!==1)throw Error('Public discovery needs one unambiguous Express application');
 const entry=join(backend,candidates[0].path);
 let source=readFileSync(entry,'utf8');const old=readJSON(record);
 if(old){if(hash(source)===old.installed_digest)return false;if(hash(source)!==old.original_digest)throw Error('Discovery entry was edited; reconcile without overwriting merchant changes');}
 const matches=[...source.matchAll(/\bconst\s+(\w+)\s*=\s*express\(\);/g)];
 if(matches.length!==1)throw Error('Public discovery requires a reviewed Express entry point; no merchant source was changed');
 const pkg=readJSON(join(frontend,'package.json')) || {};
 const deps={...pkg.dependencies,...pkg.devDependencies};
 const profileRoot=existsSync(join(frontend,'public')) || ['vite','next','nuxt'].some(name=>deps[name]) ? join(frontend,'public') : frontend;
 const match=matches[0],profile=resolve(profileRoot,'.well-known/ucp');
 const rel=relative(dirname(entry),profile).replaceAll('\\','/');
 const route=`\n// Auteric discovery route; remove using the recorded original source.\n${match[1]}.get('/.well-known/ucp',(_req,res,next)=>{try{res.set('Cache-Control','no-store').type('application/json').send(autericReadProfile(new URL(${JSON.stringify(rel)},import.meta.url),'utf8'));}catch(error){if(error.code==='ENOENT')next();else next(error);}});\n`;
 const installed="import {readFileSync as autericReadProfile} from 'node:fs';\n"+source.slice(0,match.index+match[0].length)+route+source.slice(match.index+match[0].length);
 atomicJSON(record,{entry:relative(backend,entry),original:source,original_digest:hash(source),installed_digest:hash(installed)});
 writeFileSync(entry,installed);return true;
}

export function restoreDiscoveryRoute(backend){
 const record=readJSON(join(backend,'.auteric','discovery-route.json'));
 if(!record)return false;
 const entry=join(backend,record.entry),source=readFileSync(entry,'utf8');
 if(hash(source)===record.original_digest)return true;
 if(hash(source)!==record.installed_digest)throw Error('Agent access is revoked. Merchant edits were preserved; remove only the marked discovery route manually.');
 writeFileSync(entry,record.original);return true;
}
