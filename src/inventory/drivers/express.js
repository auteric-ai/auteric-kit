// Express driver: route registration (app.METHOD, Router mounts), import-graph
// tracing to business services, auth middleware and persistence hints.
import { packageDeps, isJsFile, serviceCalls, filePersistence, fileAuth, verbRoutes, useMounts, joinPaths, attributeCalls, wrapperRoutes } from './jsShared.js';
import { matchAll, jsImports, resolveModule, isTestFile } from '../util.js';

function routerReceivers(file) {
  const receivers = new Set(['app', 'router', 'server']);
  for (const match of matchAll(file.content, /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:express\(\)|express\.Router\(\)|Router\(\))/)) {
    receivers.add(match.groups[0]);
  }
  return receivers;
}

export default {
  id: 'express',
  detect(ctx) {
    const deps = packageDeps(ctx);
    const evidence = [];
    if (deps.express) evidence.push({ type: 'dependency', file: 'package.json', detail: `express@${deps.express}` });
    for (const file of ctx.files) {
      if (!isJsFile(file.path)) continue;
      if (/require\(\s*['"]express['"]\s*\)|from\s+['"]express['"]/.test(file.content)) {
        evidence.push({ type: 'import', file: file.path, detail: 'imports express' });
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
    const mountsByFile = new Map();
    const jsFiles = ctx.files.filter(file => isJsFile(file.path) && !isTestFile(file.path));
    for (const file of jsFiles) {
      mountsByFile.set(file.path, useMounts(file));
      auth.push(...fileAuth(file));
      persistence.push(...filePersistence(file));
      for (const entry of jsImports(file.content)) if (!entry.specifier.startsWith('.')) dependencies.push(entry.specifier);
    }
    for (const file of jsFiles) {
      routes.push(...attributeCalls(verbRoutes(ctx, file, routerReceivers(file)), serviceCalls(ctx, file)));
      routes.push(...wrapperRoutes(ctx, file));
    }
    // Apply Router mounts: a mounted prefix rewrites routes in the file that
    // defines the mounted router (or the file the mounted specifier resolves to).
    for (const file of jsFiles) {
      const imports = jsImports(file.content);
      for (const mount of mountsByFile.get(file.path) || []) {
        let targetFile = null;
        const binding = imports.find(entry => entry.names.includes(mount.target));
        if (binding) targetFile = resolveModule(ctx, file.path, binding.specifier);
        if (!targetFile) {
          const defining = jsFiles.find(candidate => candidate.path !== file.path &&
            new RegExp(`(?:const|let|var)\\s+${mount.target}\\s*=\\s*(?:express\\.)?Router\\(`).test(candidate.content));
          targetFile = defining?.path || null;
        }
        if (!targetFile) continue;
        for (const route of routes) {
          if (route.file === targetFile && !route.path.startsWith(mount.prefix + '/') && route.path !== mount.prefix) {
            route.path = joinPaths(mount.prefix, route.path);
            route.mount = { prefix: mount.prefix, file: file.path, line: mount.line };
          }
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
    return { kindHint: 'backend', language: jsFiles.some(file => file.path.endsWith('.ts') || file.path.endsWith('.tsx')) ? 'typescript' : 'javascript', framework: 'express', routes, services, auth, persistence, dependencies: [...new Set(dependencies)].sort() };
  },
};
