// Next.js driver: app-router route handlers, pages/api handlers and server
// actions. A Next app without any of these is a frontend only — its presence
// never implies a backend exists.
import { isJsFile, serviceCalls, filePersistence, fileAuth } from './jsShared.js';
import { matchAll, jsImports, isTestFile } from '../util.js';

const HTTP_VERBS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

function routePathFromFile(path) {
  const match = /(?:^|\/)(?:src\/)?app\/(.+)\/route\.(?:ts|tsx|js|jsx|mjs)$/.exec(path);
  if (!match) return null;
  const segments = match[1].split('/').filter(segment => !/^\(.+\)$/.test(segment));
  return '/' + segments.map(segment => {
    const dynamic = /^\[([^\]]+)\]$/.exec(segment);
    return dynamic ? `:${dynamic[1].replace(/^\.\.\./, '')}` : segment;
  }).join('/');
}

function pagesApiPath(path) {
  const match = /(?:^|\/)(?:src\/)?pages\/api\/(.+)\.(?:ts|tsx|js|jsx|mjs)$/.exec(path);
  if (!match) return null;
  return '/api/' + match[1].replace(/\/index$/, '').replace(/\[([^\]]+)\]/g, ':$1');
}

export default {
  id: 'nextjs',
  detect(ctx) {
    const evidence = [];
    for (const file of ctx.files) {
      if (!file.path.endsWith('package.json')) continue;
      try {
        const data = JSON.parse(file.content);
        if (data.dependencies?.next || data.devDependencies?.next) {
          evidence.push({ type: 'dependency', file: file.path, detail: 'next dependency' });
        }
      } catch { /* invalid package.json is ignored */ }
    }
    for (const file of ctx.files) {
      if (/(?:^|\/)app\/.+\/route\.(?:ts|js)$/.test(file.path) || /(?:^|\/)pages\/api\//.test(file.path)) {
        evidence.push({ type: 'route_file', file: file.path, detail: 'next route handler' });
      }
      if (isJsFile(file.path) && /^\s*['"]use server['"]/.test(file.content)) {
        evidence.push({ type: 'server_action', file: file.path, detail: 'server action module' });
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
    const jsFiles = ctx.files.filter(file => isJsFile(file.path) && !isTestFile(file.path));
    for (const file of jsFiles) {
      auth.push(...fileAuth(file));
      persistence.push(...filePersistence(file));
      for (const entry of jsImports(file.content)) if (!entry.specifier.startsWith('.')) dependencies.push(entry.specifier);
      const appPath = routePathFromFile(file.path);
      if (appPath) {
        const calls = serviceCalls(ctx, file);
        for (const verb of HTTP_VERBS) {
          for (const match of matchAll(file.content, new RegExp(`export\\s+(?:async\\s+)?(?:function\\s+${verb}|const\\s+${verb}\\s*=)`))) {
            routes.push({ kind: 'rest', method: verb, path: appPath, file: file.path, line: match.line, middleware: [], auth: fileAuth(file), serviceCalls: calls });
          }
        }
      }
      const pagesPath = pagesApiPath(file.path);
      if (pagesPath) {
        const methods = matchAll(file.content, /req\.method\s*===?\s*['"]([A-Z]+)['"]/).map(match => match.groups[0]);
        for (const method of methods.length ? [...new Set(methods)] : ['ANY']) {
          routes.push({ kind: 'rest', method, path: pagesPath, file: file.path, line: 1, middleware: [], auth: fileAuth(file), serviceCalls: serviceCalls(ctx, file) });
        }
      }
      if (/^\s*['"]use server['"]/.test(file.content)) {
        const calls = serviceCalls(ctx, file);
        for (const match of matchAll(file.content, /export\s+async\s+function\s+([A-Za-z_$][\w$]*)\s*\(/)) {
          routes.push({ kind: 'server_action', method: 'POST', path: `#action/${match.groups[0]}`, file: file.path, line: match.line, symbol: match.groups[0], middleware: [], auth: fileAuth(file), serviceCalls: calls });
        }
      }
    }
    const seen = new Set();
    for (const call of routes.flatMap(route => route.serviceCalls)) {
      const key = `${call.resolved_file}#${call.symbol}`;
      if (seen.has(key)) continue;
      seen.add(key);
      services.push({ name: call.symbol, file: call.resolved_file });
    }
    const hasBackend = routes.length > 0;
    return {
      kindHint: hasBackend ? 'fullstack' : 'frontend',
      language: jsFiles.some(file => file.path.endsWith('.ts') || file.path.endsWith('.tsx')) ? 'typescript' : 'javascript',
      framework: 'next',
      routes, services, auth, persistence,
      dependencies: [...new Set(dependencies)].sort(),
    };
  },
};
