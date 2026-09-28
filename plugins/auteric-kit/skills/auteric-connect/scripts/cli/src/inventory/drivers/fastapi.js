// FastAPI driver: decorator route registration, APIRouter prefixes, Depends
// auth, service imports.
import { matchAll, pyImports, resolveModule, persistenceType, authType, isTestFile } from '../util.js';
import { attributeCalls } from './jsShared.js';

function pyFiles(ctx) {
  return ctx.files.filter(file => file.path.endsWith('.py') && !isTestFile(file.path));
}

function pyServiceCalls(ctx, file) {
  const imports = pyImports(file.content);
  const calls = [];
  for (const match of matchAll(file.content, /\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(/)) {
    const [symbol, method] = match.groups;
    const binding = imports.find(entry => entry.names.includes(symbol));
    if (!binding) continue;
    const resolved = resolveModule(ctx, file.path, binding.module);
    if (!resolved || resolved === file.path) continue;
    calls.push({ symbol, method, file: file.path, line: match.line, resolved_file: resolved });
  }
  return calls;
}

function pyAuth(file) {
  const found = [];
  for (const match of matchAll(file.content, /Depends\(\s*(\w+)\s*\)|HTTPBearer|OAuth2PasswordBearer|Security\(\s*(\w+)/)) {
    const name = match.groups.find(group => group) || match.text;
    found.push({ type: authType(name) || 'custom', file: file.path, line: match.line, detail: match.text.trim() });
  }
  return found;
}

function pyPersistence(file) {
  const found = [];
  for (const entry of pyImports(file.content)) {
    const type = persistenceType(entry.module);
    if (type) found.push({ type, file: file.path, line: entry.line, detail: `imports ${entry.module}` });
  }
  return found;
}

export default {
  id: 'fastapi',
  detect(ctx) {
    const evidence = [];
    for (const file of ctx.files) {
      if ((file.path.endsWith('requirements.txt') || file.path.endsWith('pyproject.toml')) && /\bfastapi\b/.test(file.content)) {
        evidence.push({ type: 'dependency', file: file.path, detail: 'fastapi dependency' });
      }
      if (file.path.endsWith('.py') && /from\s+fastapi\s+import|import\s+fastapi/.test(file.content)) {
        evidence.push({ type: 'import', file: file.path, detail: 'imports fastapi' });
      }
    }
    return evidence;
  },
  analyze(evidence, ctx) {
    const routes = [];
    const services = [];
    const auth = [];
    const persistence = [];
    const dependencies = [];
    for (const file of pyFiles(ctx)) {
      auth.push(...pyAuth(file));
      persistence.push(...pyPersistence(file));
      for (const entry of pyImports(file.content)) if (!entry.relative && !resolveModule(ctx, file.path, entry.module)) dependencies.push(entry.module.split('.')[0]);
      const prefixMatch = /APIRouter\(\s*prefix\s*=\s*["']([^"']+)["']/.exec(file.content);
      const includeMatch = /include_router\(\s*\w+\s*,\s*prefix\s*=\s*["']([^"']+)["']/.exec(file.content);
      const filePrefix = prefixMatch?.[1] || includeMatch?.[1] || '';
      const calls = pyServiceCalls(ctx, file);
      const fileRoutes = [];
      for (const match of matchAll(file.content, /@(app|router|\w+)\.(get|post|put|patch|delete)\(\s*["']([^"']+)["']/)) {
        const [, method, path] = match.groups;
        fileRoutes.push({
          kind: 'rest',
          method: method.toUpperCase(),
          path: (filePrefix.replace(/\/+$/, '') + '/' + path.replace(/^\/+/, '')).replace(/\/+$/, '') || '/',
          file: file.path,
          line: match.line,
          middleware: [],
          auth: pyAuth(file),
          serviceCalls: [],
        });
      }
      routes.push(...attributeCalls(fileRoutes, calls));
    }
    const seen = new Set();
    for (const call of routes.flatMap(route => route.serviceCalls)) {
      const key = `${call.resolved_file}#${call.symbol}`;
      if (seen.has(key)) continue;
      seen.add(key);
      services.push({ name: call.symbol, file: call.resolved_file });
    }
    return { kindHint: 'backend', language: 'python', framework: 'fastapi', routes, services, auth, persistence, dependencies: [...new Set(dependencies)].sort() };
  },
};
