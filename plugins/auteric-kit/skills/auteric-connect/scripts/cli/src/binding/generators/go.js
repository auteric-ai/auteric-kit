// Go generator: merchant-owned adapters under auteric/adapters_<resource>.go
// (runtime.Register calls) plus the auteric/runtime.go composition root
// (runtime.NewRuntime + http.ServeMux mount at /api/auteric/v1/). All output
// is a pure function of the binding plan.
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pascalCase } from '../naming.js';
import { addMarker } from '../util.js';

// Module path from the target repo's go.mod; service imports are module-path
// based, so the generated package location does not affect them.
function modulePath(root) {
  try {
    const gomod = readFileSync(join(root, 'go.mod'), 'utf8');
    return gomod.match(/^module\s+(\S+)/m)?.[1] || '';
  } catch { return ''; }
}

// contracts.AddToCartInput exists for every operation; the output type is the
// $ref target of the output schema (contracts.Cart) or <Operation>Output when
// the output schema is an inline object (contracts.CompleteCheckoutOutput).
function contractTypes(binding, registryPath) {
  const input = `${pascalCase(binding.operation)}Input`;
  let output = `${pascalCase(binding.operation)}Output`;
  try {
    const record = JSON.parse(readFileSync(join(registryPath, `${binding.operation}.json`), 'utf8'));
    if (record?.schemas?.output && !record.schemas.output.includes('noop')) {
      const schema = JSON.parse(readFileSync(join(registryPath, '..', '..', record.schemas.output), 'utf8'));
      if (schema.$ref) output = pascalCase(schema.$ref.split('/').pop().replace(/\.json$/, ''));
    }
  } catch { /* fall back to <Operation>Output */ }
  return { input: `contracts.${input}`, output: `contracts.${output}` };
}

function registerCall(binding, context) {
  const symbol = binding.symbol || binding.proposed_symbol;
  const types = contractTypes(binding, context.registryPath);
  const receiver = `svc.${symbol.method}`;
  const args = binding.path_params.map(param => `call.PathParams["${param}"]`);
  const lines = [];
  lines.push(`\tif err := runtime.Register(rt, "${binding.operation}", func(ctx context.Context, call runtime.Call, in ${types.input}) (${types.output}, error) {`);
  lines.push(`\t\t// TODO: reconcile this call with ${symbol.name}.${symbol.method}'s real signature${binding.route ? ` (traced from ${binding.route.method} ${binding.route.path})` : ''};`);
  lines.push(`\t\t// call.Principal carries the verified merchant-local principal — pass it`);
  lines.push(`\t\t// to your service; never read identity from the request payload.`);
  lines.push(`\t\tresult, err := ${receiver}(${args.join(', ')})`);
  lines.push(`\t\t_ = result // TODO: map the merchant result onto ${types.output}.`);
  lines.push(`\t\tif err != nil {`);
  lines.push(`\t\t\treturn ${types.output}{}, err`);
  lines.push(`\t\t}`);
  lines.push(`\t\treturn ${types.output}{}, nil`);
  lines.push(`\t}); err != nil {`);
  lines.push(`\t\treturn err`);
  lines.push(`\t}`);
  return lines.join('\n');
}

function adapterFile(resource, bindings, context) {
  const path = bindings[0].adapter_file;
  const symbols = new Map();
  for (const binding of bindings) {
    const symbol = binding.symbol || binding.proposed_symbol;
    if (symbol) symbols.set(`${symbol.file}#${symbol.name}`, symbol);
  }
  const imports = [
    '"context"',
    '"github.com/auteric-ai/merchant-go/contracts"',
    '"github.com/auteric-ai/merchant-go/runtime"',
  ];
  const serviceParams = [];
  for (const symbol of [...symbols.values()].sort((a, b) => (a.file + a.name < b.file + b.name ? -1 : 1))) {
    const dir = symbol.file.split('/').slice(0, -1).join('/');
    const pkg = dir.split('/').pop();
    imports.push(`${pkg} "${context.module}/${dir}"`);
    serviceParams.push(`svc *${pkg}.${symbol.name}`);
  }
  const body = `// ${path} — merchant-owned Auteric adapters for the "${resource}" resource.
// Generated scaffold from the locked contracts registry; review the TODOs,
// then own this file. Rerunning \`auteric bind\` never overwrites hand edits.
package auteric

import (
${imports.map(line => `\t${line}`).join('\n')}
)

// Register${pascalCase(resource)}Adapters wires the ${bindings.map(binding => binding.operation).join(', ')} adapter(s) into the runtime.
func Register${pascalCase(resource)}Adapters(rt *runtime.Runtime${serviceParams.length ? ', ' + serviceParams.join(', ') : ''}) error {
${bindings.map(binding => registerCall(binding, context)).join('\n')}
\treturn nil
}
`;
  return { path, content: addMarker(path, body), kind: 'adapter', operations: bindings.map(binding => binding.operation) };
}

function compositionRoot(plan, context) {
  const path = plan.composition_root;
  const resources = [...new Set(plan.bindings.map(binding => binding.resource))].sort();
  const symbols = new Map();
  for (const binding of plan.bindings) {
    const symbol = binding.symbol || binding.proposed_symbol;
    if (symbol) symbols.set(`${symbol.file}#${symbol.name}`, symbol);
  }
  const imports = [
    '"net/http"',
    '"github.com/auteric-ai/merchant-go/execstore"',
    '"github.com/auteric-ai/merchant-go/principal"',
    '"github.com/auteric-ai/merchant-go/runtime"',
  ];
  const serviceParams = [];
  for (const symbol of [...symbols.values()].sort((a, b) => (a.file + a.name < b.file + b.name ? -1 : 1))) {
    const dir = symbol.file.split('/').slice(0, -1).join('/');
    const pkg = dir.split('/').pop();
    imports.push(`${pkg} "${context.module}/${dir}"`);
    serviceParams.push(`svc *${pkg}.${symbol.name}`);
  }
  const operations = plan.bindings.map(binding => binding.operation).sort();
  const registrations = resources.map(resource => `\tif err := Register${pascalCase(resource)}Adapters(rt, svc); err != nil {\n\t\treturn nil, err\n\t}`);
  const body = `// ${path} — Auteric composition root (generated).
// Only manifest-listed operations are routed; anything else under
// /api/auteric/v1/ is denied by the runtime. See the merchant-go README.
package auteric

import (
${imports.map(line => `\t${line}`).join('\n')}
)

// Operations bound by this installation; contract and binding digests are
// pinned in .auteric/installation.json.
var Operations = []string{${operations.map(operation => `"${operation}"`).join(', ')}}

// BuildRuntime constructs the MEP/1 runtime and registers every bound
// adapter. TODO: execstore.NewMemoryStore and principal.MapResolver are
// dev-only; back them with durable implementations (merchant-go README,
// idempotency-store section) before going live.
func BuildRuntime(cfg runtime.Config, ${serviceParams.join(', ')}) (*runtime.Runtime, error) {
	rt, err := runtime.NewRuntime(cfg, map[string]runtime.Adapter{}, execstore.NewMemoryStore(), principal.MapResolver{})
	if err != nil {
		return nil, err
	}
${registrations.join('\n')}
	return rt, nil
}

// Mount registers the MEP/1 handler. Add a /.well-known/ucp handler from the
// merchant-go discovery package next to it.
func Mount(mux *http.ServeMux, rt *runtime.Runtime) {
	mux.Handle("/api/auteric/v1/", rt.Handler())
}
`;
  return { path, content: addMarker(path, body), kind: 'composition', operations };
}

export function generateGo(plan, context = {}) {
  const root = context.root || '.';
  const ctx = { root, module: modulePath(root), registryPath: plan.registry.path };
  const artifacts = [];
  const byResource = new Map();
  for (const binding of plan.bindings) {
    if (!byResource.has(binding.resource)) byResource.set(binding.resource, []);
    byResource.get(binding.resource).push(binding);
  }
  for (const resource of [...byResource.keys()].sort()) {
    artifacts.push(adapterFile(resource, byResource.get(resource), ctx));
  }
  artifacts.push(compositionRoot(plan, ctx));
  return artifacts;
}
