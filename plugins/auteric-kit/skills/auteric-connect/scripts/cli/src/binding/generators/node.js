// Node/TypeScript generator: merchant-owned adapter files under
// <top>/auteric/adapters/<resource>.ts plus the <top>/auteric/index.ts
// composition root (createMerchantRuntime + createAutericRouter mounted at
// /api/auteric/v1 and a /.well-known/ucp handler). All output is a pure
// function of the binding plan, so reruns are byte-identical.
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { camelCase, lowerFirst, tsType } from '../naming.js';
import { addMarker } from '../util.js';
import { nodeObjectCall } from '../node-call.js';

function posix(path) {
  return path.split('\\').join('/');
}

function importSpecifier(adapterFile, targetFile) {
  let specifier = posix(relative(dirname(adapterFile), targetFile));
  if (!specifier.startsWith('.')) specifier = './' + specifier;
  return specifier;
}

// ESM named export vs CommonJS: decides the import shape the adapter uses.
function esmExport(root, file, name) {
  try {
    if (!existsSync(join(root, file))) return true; // proposed files: assume ESM scaffold
    const content = readFileSync(join(root, file), 'utf8');
    if (new RegExp(`^\\s*export\\s+(?:async\\s+)?(?:class|function|const|let|var)\\s+${name}\\b`, 'm').test(content)) return true;
    if (new RegExp(`^\\s*export\\s*\\{[^}]*\\b${name}\\b`, 'm').test(content)) return true;
    return false;
  } catch { return true; }
}

function inputTypeLiteral(binding, indent) {
  if (!binding.input_fields.length) return null;
  const lines = binding.input_fields.map(field => `${indent}  ${field.name}${field.required ? '' : '?'}: ${field.type};`);
  return `{\n${lines.join('\n')}\n${indent}}`;
}

// The traced call arguments: verified principal first, then contract path
// parameters, then contract input fields (camelCased), then concurrency and
// idempotency context when the contract requires them.
function callArguments(binding, indent, inputVar) {
  const args = ['principal: ctx.principal, // resolved merchant principal (verified token, never the body)'];
  for (const param of binding.path_params) args.push(`${camelCase(param)}: ctx.pathParams.${param},`);
  for (const field of binding.input_fields) {
    if (field.name === 'expected_revision') continue;
    const target = field.name === 'q' && binding.call_fields?.includes('query') && !binding.call_fields.includes('q') ? 'query' : camelCase(field.name);
    args.push(`${target}: ${inputVar}.${field.name},`);
  }
  if (binding.contract.concurrency_mode === 'resource_revision') args.push('expectedRevision: ctx.expectedRevision,');
  if (binding.contract.idempotency === 'required') args.push('idempotencyKey: ctx.actionId,');
  return `{\n${args.map(arg => `${indent}  ${arg}`).join('\n')}\n${indent}}`;
}

function adapterBody(binding, serviceVar, indent) {
  const lines = [];
  const type = inputTypeLiteral(binding, indent);
  const inputVar = type ? 'i' : 'input';
  if (type) lines.push(`${indent}const i = input as ${type};`);
  else lines.push(`${indent}void input;`);
  const traced = binding.route ? ` (traced from ${binding.route.method} ${binding.route.path})` : '';
  lines.push(`${indent}// TODO: reconcile this object call with ${binding.symbol.name}.${binding.symbol.method}'s real signature${traced}.`);
  lines.push(`${indent}return ${serviceVar}.${binding.symbol.method}(${callArguments(binding, indent, inputVar)});`);
  return lines.join('\n');
}

function internalApiBody(binding, indent) {
  if (!binding.internal_api?.route) {
    throw new Error(`${binding.operation}: refusing to generate an internal API adapter without a proven merchant route mapping`);
  }
  const lines = [];
  const type = inputTypeLiteral(binding, indent);
  if (type) lines.push(`${indent}const i = input as ${type};`);
  else lines.push(`${indent}void input;`);
  const fields = binding.input_fields.filter(field => field.name !== 'expected_revision');
  const inputVar = type ? 'i' : 'input';
  const route = binding.internal_api.route;
  const path = route.path.replace(/(:|\{)([A-Za-z_]\w*)\}?/g, (_, _prefix, merchantParam) => {
    const contractParam = route.path_params?.[merchantParam];
    if (!contractParam) throw new Error(`${binding.operation}: missing proven mapping for merchant path parameter ${merchantParam}`);
    return `\${encodeURIComponent(String(ctx.pathParams.${contractParam}))}`;
  });
  lines.push(`${indent}// Internal API binding: traced ${route.method} ${route.path}; all route and field mappings were evidenced during binding.`);
  lines.push(`${indent}// Service credentials come from the environment, never from request data.`);
  lines.push(`${indent}const url = new URL(\`${path}\`, process.env.INTERNAL_API_BASE_URL);`);
  if (binding.internal_api.input_transport === 'query') {
    for (const field of fields) {
      lines.push(`${indent}url.searchParams.set("${field.name}", typeof ${inputVar}.${field.name} === "string" ? ${inputVar}.${field.name} : JSON.stringify(${inputVar}.${field.name}));`);
    }
  }
  lines.push(`${indent}const response = await fetch(url, {`);
  lines.push(`${indent}  method: "${route.method}",`);
  lines.push(`${indent}  headers: {`);
  if (binding.internal_api.input_transport === 'json_body') lines.push(`${indent}    "content-type": "application/json",`);
  lines.push(`${indent}    authorization: \`Bearer \${process.env.INTERNAL_API_SERVICE_TOKEN ?? ""}\`,`);
  lines.push(`${indent}  },`);
  if (binding.internal_api.input_transport === 'json_body') {
    const payload = ['principal: ctx.principal', ...fields.map(field => `${field.name}: ${inputVar}.${field.name}`)];
    lines.push(`${indent}  body: JSON.stringify({ ${payload.join(', ')} }),`);
  }
  lines.push(`${indent}});`);
  lines.push(`${indent}if (!response.ok) throw new Error(\`internal API returned \${response.status}\`);`);
  lines.push(`${indent}return response.json();`);
  return lines.join('\n');
}

function adapterFile(resource, bindings, plan, root) {
  const path = plan.bindings.find(binding => binding.resource === resource).adapter_file;
  const symbols = new Map();
  for (const binding of bindings) {
    const symbol = binding.symbol || binding.proposed_symbol;
    if (symbol && binding.action !== 'bind_internal_api') symbols.set(`${symbol.file}#${symbol.name}`, symbol);
  }
  const imports = [];
  const locals = [];
  for (const symbol of [...symbols.values()].sort((a, b) => (a.file + a.name < b.file + b.name ? -1 : 1))) {
    const specifier = importSpecifier(path, symbol.file);
    if (esmExport(root, symbol.file, symbol.name)) {
      imports.push(`import { ${symbol.name} } from "${specifier}";`);
    } else {
      const moduleVar = `${lowerFirst(symbol.name)}Module`;
      imports.push(`import ${moduleVar} from "${specifier}";`);
      locals.push(`const { ${symbol.name} } = ${moduleVar};`);
    }
  }
  const serviceType = [...symbols.values()].map(symbol => `${lowerFirst(symbol.name)}?: typeof ${symbol.name}`).join('; ');
  const header = `// ${path} — merchant-owned Auteric adapters for the "${resource}" resource.
// Generated scaffold from the locked contracts registry; review the TODOs,
// then own this file. Rerunning \`auteric bind\` never overwrites hand edits.
//
// Conventions:
//   - Contract input fields are snake_case; merchant call arguments are
//     camelCased (product_id -> productId). Path parameters arrive in
//     ctx.pathParams under their contract names and are passed camelCased.
//   - Identity comes only from ctx.principal (verified execution token);
//     never read a principal from the request body.`;
  const entries = bindings.map(binding => {
    const indent = '      ';
    let body;
    if (binding.action === 'bind_internal_api') body = internalApiBody(binding, indent);
    else {
      const symbol = binding.symbol || binding.proposed_symbol;
      body = adapterBody({ ...binding, symbol, call_fields: nodeObjectCall(root, symbol).fields }, lowerFirst(symbol.name), indent);
    }
    return `    ${binding.operation}: async (ctx: VerifiedContext, input: unknown) => {\n${body}\n    },`;
  });
  const destructure = [...symbols.values()].map(symbol => `  const ${lowerFirst(symbol.name)} = services.${lowerFirst(symbol.name)} ?? ${symbol.name};`);
  const body = `${header}
import type { VerifiedContext } from "@auteric/merchant-node";
${imports.join('\n')}
${locals.length ? locals.join('\n') + '\n' : ''}
export function ${resource}Adapters(services: { ${serviceType} } = {}) {
${destructure.join('\n')}
  return {
${entries.join('\n')}
  };
}
`;
  return { path, content: addMarker(path, body), kind: 'adapter', operations: bindings.map(binding => binding.operation) };
}

function extractionPatch(binding, plan, root) {
  const proposed = binding.proposed_symbol;
  let original = '';
  try {
    if (binding.route?.file && existsSync(join(root, binding.route.file))) original = readFileSync(join(root, binding.route.file), 'utf8');
  } catch { /* reference excerpt stays empty */ }
  const extension = proposed.file.endsWith('.py') ? 'py' : proposed.file.endsWith('.go') ? 'go' : 'ts';
  const skeleton = extension === 'py'
    ? `class ${proposed.name}:\n    """Extracted from the inline handler in ${binding.route?.file}:${binding.route?.line}."""\n\n    @staticmethod\n    def ${proposed.method}(principal, **kwargs):\n        # TODO: move the inline handler body here unchanged (see original below).\n        raise NotImplementedError\n`
    : extension === 'go'
      ? `package ${binding.resource}\n\n// ${proposed.name} was extracted from the inline handler in ${binding.route?.file}:${binding.route?.line}.\ntype ${proposed.name} struct{}\n\n// TODO: move the inline handler body here unchanged (see original below).\nfunc (s *${proposed.name}) ${proposed.method}() error {\n\treturn nil\n}\n`
      : `import pg from "pg";\n\nconst pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });\n\nexport class ${proposed.name} {\n  // Extracted from the inline handler in ${binding.route?.file}:${binding.route?.line}.\n  static async ${proposed.method}(args: { principal: string }) {\n    // TODO: move the inline handler body here unchanged (see original below).\n    void args;\n    void pool;\n    throw new Error("not implemented");\n  }\n}\n`;
  const body = `# Service extraction proposal: ${binding.operation}

The route \`${binding.route?.method} ${binding.route?.path}\` (${binding.route?.file}${binding.route?.line ? `:${binding.route.line}` : ''})
runs its business logic inline, so no business service symbol could be traced.
Nothing was moved automatically — this proposal is for review. Apply it
manually, then rerun \`auteric bind\` so the validator can resolve the import.

## 1. Create \`${proposed.file}\`

\`\`\`${extension}
${skeleton}\`\`\`

## 2. Update the controller

\`${binding.route?.file}\` keeps its HTTP concerns (session check, request
parsing, response shape) and calls \`${proposed.name}.${proposed.method}\`.
The generated adapter \`${binding.adapter_file}\` calls the same symbol, so the
old browser flow and the new agent flow share one code path.

## 3. Prove the browser flow

Fill in the regression stub at \`${binding.regression_stub}\` and run it before
and after the extraction; the pre-extraction response shape must not change.

## Original handler file (for reference)

\`\`\`
${original}\`\`\`
`;
  return { path: binding.extraction_patch, content: addMarker(binding.extraction_patch, body), kind: 'patch', operations: [binding.operation] };
}

function regressionStub(binding) {
  const body = `// Regression stub for the browser flow of ${binding.route?.method} ${binding.route?.path}.
// Generated alongside the extraction proposal (${binding.extraction_patch}).
// Wire the app fixture and assertions, then remove the todo flag — this test
// must pass BEFORE and AFTER the service extraction lands.
import { test } from "node:test";

test("browser flow ${binding.route?.method} ${binding.route?.path} unchanged after extraction", { todo: "wire the app fixture" }, async () => {
  // 1. Start the merchant app against a test database.
  // 2. Establish a session the way the browser does.
  // 3. Exercise ${binding.route?.method} ${binding.route?.path} and assert the
  //    pre-extraction response status and body shape.
});
`;
  return { path: binding.regression_stub, content: addMarker(binding.regression_stub, body), kind: 'test', operations: [binding.operation] };
}

function compositionRoot(plan) {
  const path = plan.composition_root;
  const resources = [...new Set(plan.bindings.map(binding => binding.resource))].sort();
  const imports = resources.map(resource => `import { ${resource}Adapters } from "${importSpecifier(path, plan.bindings.find(binding => binding.resource === resource).adapter_file).replace(/\.ts$/, '.js')}";`);
  const spreads = resources.map(resource => `      ...${resource}Adapters(services),`);
  const operations = plan.bindings.map(binding => binding.operation).sort();
  const body = `// ${path} — Auteric composition root (generated).
// Mount order is a security property: the Auteric router must be mounted
// BEFORE express.json() (the MEP/1 §3 request hash covers the raw body
// bytes) and BEFORE any SPA catch-all. See the @auteric/merchant-node README.
import express from "express";
import {
  createDiscoveryHandler,
  createMerchantRuntime,
  InMemoryExecutionStore,
  MapPrincipalResolver,
} from "@auteric/merchant-node";
import { createAutericRouter, createExpressDiscoveryHandler } from "@auteric/merchant-node/express";
${imports.join('\n')}

// Operations bound by this installation; contract and binding digests are
// pinned in .auteric/installation.json.
export const AUTERIC_OPERATIONS = [${operations.map(operation => `"${operation}"`).join(', ')}] as const;

export function createAutericApp(services: any, config: any) {
  const runtime = createMerchantRuntime({
    installation: config.installation,
    trust: config.trust,
    adapters: {
${spreads.join('\n')}
    },
    // TODO: InMemoryExecutionStore and MapPrincipalResolver are dev-only; back
    // executionStore and principalResolver with durable stores (SDK README,
    // production notes) before going live.
    executionStore: new InMemoryExecutionStore(),
    principalResolver: new MapPrincipalResolver(config.principals ?? {}),
  });
  const discovery = createDiscoveryHandler({
    fetcher: config.discoveryFetcher,
    trust: config.trust,
    storeId: config.storeId,
    hostname: config.hostname,
  });
  const app = express();
  app.use("/api/auteric/v1", createAutericRouter(runtime));
  app.get("/.well-known/ucp", createExpressDiscoveryHandler(discovery));
  return app;
}
`;
  return { path, content: addMarker(path, body), kind: 'composition', operations };
}

export function generateNode(plan, context = {}) {
  const root = context.root || plan.repo || '.';
  const artifacts = [];
  const byResource = new Map();
  for (const binding of plan.bindings) {
    if (!byResource.has(binding.resource)) byResource.set(binding.resource, []);
    byResource.get(binding.resource).push(binding);
  }
  for (const resource of [...byResource.keys()].sort()) {
    artifacts.push(adapterFile(resource, byResource.get(resource), plan, root));
  }
  artifacts.push(compositionRoot(plan));
  for (const binding of plan.bindings) {
    if (binding.action !== 'extract_service') continue;
    artifacts.push(extractionPatch(binding, plan, root));
    artifacts.push(regressionStub(binding));
  }
  return artifacts;
}
