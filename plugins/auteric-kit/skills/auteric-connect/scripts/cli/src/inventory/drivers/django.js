// Django driver: urls.py routing tables and views calling service modules.
import { matchAll, pyImports, resolveModule, persistenceType, authType, isTestFile } from '../util.js';

function pyFiles(ctx) {
  return ctx.files.filter(file => file.path.endsWith('.py') && !isTestFile(file.path));
}

export default {
  id: 'django',
  detect(ctx) {
    const evidence = [];
    for (const file of ctx.files) {
      if (file.path.endsWith('manage.py')) evidence.push({ type: 'entrypoint', file: file.path, detail: 'django manage.py' });
      if (file.path.endsWith('.py') && /from\s+django|import\s+django/.test(file.content)) {
        evidence.push({ type: 'import', file: file.path, detail: 'imports django' });
      }
      if ((file.path.endsWith('requirements.txt') || file.path.endsWith('pyproject.toml')) && /\bdjango\b/i.test(file.content)) {
        evidence.push({ type: 'dependency', file: file.path, detail: 'django dependency' });
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
    const viewModules = new Map(); // module name -> file, for views referenced from urls.py
    for (const file of pyFiles(ctx)) {
      const moduleName = file.path.replace(/\.py$/, '').replace(/\//g, '.').split('.').slice(-2).join('.');
      viewModules.set(moduleName.split('.').pop(), file.path);
      for (const entry of pyImports(file.content)) {
        const type = persistenceType(entry.module);
        if (type) persistence.push({ type, file: file.path, line: entry.line, detail: `imports ${entry.module}` });
        if (!entry.relative && !resolveModule(ctx, file.path, entry.module)) dependencies.push(entry.module.split('.')[0]);
      }
    }
    for (const file of pyFiles(ctx)) {
      if (!/(^|\/)urls\.py$/.test(file.path)) continue;
      const prefixMatch = null; // Django include() prefixes handled below
      for (const match of matchAll(file.content, /(?:path|re_path)\(\s*["']([^"']+)["']\s*,\s*([\w.]+)(?:\s*,\s*\{[^}]*\})?\s*\)/)) {
        const [rawPath, target] = match.groups;
        const segments = target.split('.');
        const handler = segments.pop();
        const module = segments.pop() || 'views';
        const viewFile = viewModules.get(module) || null;
        let method = 'ANY';
        let viewAuth = [];
        let calls = [];
        if (viewFile) {
          const content = ctx.files.find(candidate => candidate.path === viewFile)?.content || '';
          const apiView = new RegExp(`@api_view\\(\\[([^\\]]+)\\]\\)(?:(?!@api_view)[\\s\\S]){0,200}?def\\s+${handler}\\b`).exec(content);
          if (apiView) method = (/'([A-Z]+)'|"([A-Z]+)"/.exec(apiView[1])?.slice(1).find(Boolean)) || 'ANY';
          const requirePost = new RegExp(`@require_POST(?:(?!@require_)[\\s\\S]){0,200}?def\\s+${handler}\\b`).exec(content);
          if (requirePost) method = 'POST';
          const functionBody = new RegExp(`def\\s+${handler}\\s*\\([\\s\\S]*?(?=\\ndef\\s|\\nclass\\s|$)`).exec(content);
          const body = functionBody?.[0] || '';
          const imports = pyImports(content);
          for (const call of matchAll(body, /\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(/)) {
            const binding = imports.find(entry => entry.names.includes(call.groups[0]));
            if (!binding) continue;
            const resolved = resolveModule(ctx, viewFile, binding.module);
            if (resolved && resolved !== viewFile) calls.push({ symbol: call.groups[0], method: call.groups[1], file: viewFile, line: call.line, resolved_file: resolved });
          }
          if (new RegExp(`@login_required(?:(?!def\\s)[\\s\\S]){0,200}?def\\s+${handler}\\b`).test(content)) {
            viewAuth.push({ type: 'session', file: viewFile, line: 1, detail: 'login_required' });
          }
          if (/IsAuthenticated|permission_classes/.test(content)) {
            viewAuth.push({ type: authType(content) || 'custom', file: viewFile, line: 1, detail: 'permission_classes' });
          }
        }
        routes.push({
          kind: 'rest',
          method,
          path: '/' + rawPath.replace(/^\/+/, ''),
          file: file.path,
          line: match.line,
          handler,
          view_file: viewFile,
          middleware: [],
          auth: viewAuth,
          serviceCalls: calls,
        });
        void prefixMatch;
      }
    }
    const seen = new Set();
    for (const call of routes.flatMap(route => route.serviceCalls)) {
      const key = `${call.resolved_file}#${call.symbol}`;
      if (seen.has(key)) continue;
      seen.add(key);
      services.push({ name: call.symbol, file: call.resolved_file });
    }
    return { kindHint: 'backend', language: 'python', framework: 'django', routes, services, auth, persistence, dependencies: [...new Set(dependencies)].sort() };
  },
};
