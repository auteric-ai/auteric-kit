import { readFileSync } from 'node:fs';
import { resolve, relative } from 'node:path';
import { expressHTTPGaps } from './diagnostics.js';
import { shared } from './shared.js';

/** OpenAPI supplies route provenance and explicit field profiles. Route names alone never select an operation. */
export async function mappingFromOpenAPI(path) {
  const { validateMapping, relativePath } = await shared('mapping');
  const document=JSON.parse(readFileSync(path,'utf8'));
  if (!/^3\./.test(document.openapi) || !document['x-auteric']) throw Error('implementation_required: OpenAPI needs explicit x-auteric session and output field profiles');
  const metadata=document['x-auteric'];
  if(Object.keys(metadata).some(k=>!['registry_digest','identity_profile','session','profiles'].includes(k)))throw Error('unknown OpenAPI mapping metadata');
  const operations=[];
  for(const [path, methods] of Object.entries(document.paths || {})) {
    relativePath(path);
    for(const [method,spec] of Object.entries(methods)) {
      if(!spec || typeof spec!=='object' || !spec['x-auteric'])continue;
      if(/admin|payment|checkout|webhook|refund/i.test(path)) throw Error('unsupported route scope in MVP mapping');
      const {operationId:operation}=spec;
      if(Object.keys(spec['x-auteric']).some(k=>!['input','output','auth','idempotency','revision'].includes(k)))throw Error('OpenAPI mapping cannot override route provenance');
      operations.push({...spec['x-auteric'],operation,target:{method:method.toUpperCase(),path}});
    }
  }
  const mapping=validateMapping({schema:'auteric-bridge-mapping/v1',...metadata,operations});
  if (!document.paths?.[mapping.session.path]?.post) throw Error('session issuer lacks OpenAPI route provenance');
  return mapping;
}
export async function selectMapping(root, options, inventory) {
  const {validateMapping}=await shared('mapping');
  const specs=[...new Set((inventory?.graph?.nodes || []).flatMap(n=>n.routes || []).filter(r=>r.kind==='openapi' && r.file.endsWith('.json')).map(r=>r.file))];
  if (options.mapping) {
    // The file is local operator-selected configuration, never cloud executable code.
    const path=resolve(root,options.mapping);
    const raw=JSON.parse(readFileSync(path,'utf8'));
    if(raw.openapi)return {mapping:await mappingFromOpenAPI(path),source:relative(root,path)};
    const mapping=validateMapping(raw);
    const routes=(inventory?.graph?.nodes || []).flatMap(n=>n.routes || []);
    if(!routes.some(r=>r.method===mapping.session.method && r.path===mapping.session.path))throw Error('implementation_required: no route provenance for merchant session issuer');
    for(const op of mapping.operations) {
      if(/admin|payment|checkout|webhook|refund/i.test(op.target.path))throw Error('unsupported route scope in MVP mapping');
      const normalize=p=>p.replace(/:([A-Za-z][A-Za-z0-9_]*)/g,'{$1}');
      if(!routes.some(r=>r.method===op.target.method && normalize(r.path)===op.target.path)) throw Error(`implementation_required: no route provenance for ${op.operation}; use an explicit OpenAPI document`);
    }
    return {mapping,source:relative(root,path)};
  }
  if(specs.length===1)return {mapping:await mappingFromOpenAPI(resolve(root,specs[0])),source:specs[0]};
  const gaps=expressHTTPGaps(root,inventory);
  if(gaps.length)throw Error('implementation_required: '+gaps.join('; ')+'; provide explicit output profiles with --mapping PATH');
  throw Error(specs.length>1?'selection_required: select the authoritative OpenAPI with --mapping PATH':'implementation_required: provide OpenAPI field profiles with --mapping PATH; no business code or coding agent was generated');
}
