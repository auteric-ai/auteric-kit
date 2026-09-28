// OpenAPI driver: an openapi.json/yaml document yields candidate operations.
// JSON is parsed fully; YAML path extraction is a documented heuristic
// (stdlib has no YAML parser) and is marked as such in the evidence.
import { matchAll } from '../util.js';

const METHODS = ['get', 'post', 'put', 'patch', 'delete'];

export default {
  id: 'openapi',
  detect(ctx) {
    const evidence = [];
    for (const file of ctx.files) {
      if (!/openapi\.(json|ya?ml)$/.test(file.path)) continue;
      if (file.path.endsWith('.json') && /"openapi"\s*:/.test(file.content)) evidence.push({ type: 'spec', file: file.path, detail: 'openapi json' });
      if (/\.ya?ml$/.test(file.path) && /^openapi\s*:/m.test(file.content)) evidence.push({ type: 'spec', file: file.path, detail: 'openapi yaml (heuristic parse)' });
    }
    return evidence;
  },
  analyze(evidence, ctx) {
    const routes = [];
    for (const item of evidence) {
      const file = ctx.files.find(candidate => candidate.path === item.file);
      if (!file) continue;
      if (file.path.endsWith('.json')) {
        let spec;
        try { spec = JSON.parse(file.content); } catch { continue; }
        for (const [path, verbs] of Object.entries(spec.paths || {})) {
          for (const method of Object.keys(verbs || {})) {
            if (!METHODS.includes(method)) continue;
            const operation = verbs[method] || {};
            routes.push({
              kind: 'openapi',
              method: method.toUpperCase(),
              path,
              file: file.path,
              line: 1,
              symbol: operation.operationId || null,
              middleware: [],
              auth: operation.security ? [{ type: 'custom', file: file.path, line: 1, detail: 'spec security requirement' }] : [],
              serviceCalls: [],
            });
          }
        }
        continue;
      }
      // YAML heuristic: two-space paths, four-space verbs under `paths:`.
      let inPaths = false;
      let currentPath = null;
      for (const match of matchAll(file.content, /^(\s*)(\/\S+|paths:|[a-z]+:)\s*$/m)) {
        const [indent, key] = match.groups;
        if (key === 'paths:') { inPaths = true; continue; }
        if (!inPaths) continue;
        if (indent === '' && key.endsWith(':')) { inPaths = false; continue; }
        if (indent === '  ' && key.startsWith('/')) { currentPath = key.slice(0, -1); continue; }
        if (indent === '    ' && currentPath) {
          const method = key.slice(0, -1);
          if (!METHODS.includes(method)) continue;
          routes.push({ kind: 'openapi', method: method.toUpperCase(), path: currentPath, file: file.path, line: match.line, symbol: null, middleware: [], auth: [], serviceCalls: [], yaml_heuristic: true });
        }
      }
    }
    return { kindHint: 'backend', language: null, framework: 'openapi', routes, services: [], auth: [], persistence: [], dependencies: [] };
  },
};
