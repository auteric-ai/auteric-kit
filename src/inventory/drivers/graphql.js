// GraphQL driver: resolver fields that call business services. A GraphQL
// resolver and a REST route hitting the same service are ONE capability; this
// driver emits entrypoints, the candidate matcher deduplicates.
import { packageDeps, isJsFile, serviceCalls, filePersistence, fileAuth } from './jsShared.js';
import { matchAll, isTestFile } from '../util.js';

export default {
  id: 'graphql',
  detect(ctx) {
    const deps = packageDeps(ctx);
    const evidence = [];
    const dep = Object.keys(deps).find(name => /^(graphql|@apollo\/|apollo-server|graphql-yoga|@graphql-tools\/|mercurius)/.test(name));
    if (dep) evidence.push({ type: 'dependency', file: 'package.json', detail: dep });
    for (const file of ctx.files) {
      if (/\.(graphql|gql)$/.test(file.path)) evidence.push({ type: 'schema', file: file.path, detail: 'graphql schema' });
      if (isJsFile(file.path) && /\btype\s+(Query|Mutation)\b|makeExecutableSchema|resolvers\s*=\s*\{/.test(file.content)) {
        evidence.push({ type: 'resolver', file: file.path, detail: 'graphql resolver map' });
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
    for (const file of ctx.files) {
      if (!isJsFile(file.path) || isTestFile(file.path)) continue;
      auth.push(...fileAuth(file));
      persistence.push(...filePersistence(file));
      const calls = serviceCalls(ctx, file);
      // Resolver fields: `Mutation: { fieldName: (...) => service.method(...) }`
      // and Query fields. Records one entrypoint per field.
      for (const block of matchAll(file.content, /\b(Query|Mutation|Subscription)\s*:\s*\{([\s\S]*?)\n\s*\}/)) {
        const [operationType, body] = block.groups;
        for (const field of matchAll(body, /^\s*(\w+)\s*[:(]/m)) {
          const fieldCalls = calls.filter(call => call.line >= block.line);
          routes.push({
            kind: 'graphql',
            method: operationType === 'Query' ? 'GET' : 'POST',
            path: `#graphql/${field.groups[0]}`,
            symbol: field.groups[0],
            operation_type: operationType.toLowerCase(),
            file: file.path,
            line: block.line + (field.line - 1),
            middleware: [],
            auth: auth.filter(entry => entry.file === file.path),
            serviceCalls: fieldCalls,
          });
        }
      }
      for (const entry of (file.content.match(/from\s+['"]([^'"]+)['"]/g) || [])) {
        const specifier = /from\s+['"]([^'"]+)['"]/.exec(entry)[1];
        if (!specifier.startsWith('.')) dependencies.push(specifier);
      }
    }
    const seen = new Set();
    for (const call of routes.flatMap(route => route.serviceCalls)) {
      const key = `${call.resolved_file}#${call.symbol}`;
      if (seen.has(key)) continue;
      seen.add(key);
      services.push({ name: call.symbol, file: call.resolved_file });
    }
    return { kindHint: 'backend', language: 'javascript', framework: 'graphql', routes, services, auth, persistence, dependencies: [...new Set(dependencies)].sort() };
  },
};
