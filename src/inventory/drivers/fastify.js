// Fastify driver: fastify.METHOD registrations and register() prefixes.
import { packageDeps, isJsFile, serviceCalls, filePersistence, fileAuth, verbRoutes, joinPaths, attributeCalls } from './jsShared.js';
import { matchAll, jsImports, isTestFile } from '../util.js';

function fastifyReceivers(file) {
  const receivers = new Set(['fastify', 'app', 'server']);
  for (const match of matchAll(file.content, /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:fastify|Fastify)\s*\(/)) {
    receivers.add(match.groups[0]);
  }
  return receivers;
}

export default {
  id: 'fastify',
  detect(ctx) {
    const deps = packageDeps(ctx);
    const evidence = [];
    if (deps.fastify) evidence.push({ type: 'dependency', file: 'package.json', detail: `fastify@${deps.fastify}` });
    for (const file of ctx.files) {
      if (isJsFile(file.path) && /require\(\s*['"]fastify['"]\s*\)|from\s+['"]fastify['"]/.test(file.content)) {
        evidence.push({ type: 'import', file: file.path, detail: 'imports fastify' });
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
    const prefixes = [];
    for (const file of jsFiles) {
      auth.push(...fileAuth(file));
      persistence.push(...filePersistence(file));
      for (const entry of jsImports(file.content)) if (!entry.specifier.startsWith('.')) dependencies.push(entry.specifier);
      for (const match of matchAll(file.content, /\.register\(\s*([A-Za-z_$][\w$]*)\s*,\s*\{\s*prefix:\s*['"`](\/[^'"`]*)['"`]/)) {
        prefixes.push({ plugin: match.groups[0], prefix: match.groups[1], file: file.path });
      }
    }
    for (const file of jsFiles) {
      routes.push(...attributeCalls(verbRoutes(ctx, file, fastifyReceivers(file)), serviceCalls(ctx, file)));
    }
    // fastify.register(plugin, { prefix }) applies to routes exported by the
    // plugin module; apply it when the plugin binding resolves to a read file.
    for (const file of jsFiles) {
      const imports = jsImports(file.content);
      for (const entry of prefixes) {
        if (entry.file !== file.path) continue;
        const binding = imports.find(item => item.names.includes(entry.plugin));
        if (!binding) continue;
        const target = binding.specifier.replace(/^\.\//, '').replace(/\.[^.]+$/, '');
        for (const route of routes) {
          if (route.file.replace(/\.[^.]+$/, '').endsWith(target) && !route.path.startsWith(entry.prefix + '/')) {
            route.path = joinPaths(entry.prefix, route.path);
            route.mount = { prefix: entry.prefix, file: file.path };
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
    return { kindHint: 'backend', language: jsFiles.some(file => file.path.endsWith('.ts')) ? 'typescript' : 'javascript', framework: 'fastify', routes, services, auth, persistence, dependencies: [...new Set(dependencies)].sort() };
  },
};
