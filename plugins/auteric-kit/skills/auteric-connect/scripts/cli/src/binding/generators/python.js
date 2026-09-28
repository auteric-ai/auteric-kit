// Python generator: merchant-owned adapters under auteric_bindings/adapters/
// plus the auteric_bindings/runtime.py composition root (MerchantRuntime +
// FastAPI include_router). All output is a pure function of the binding plan.
import { addMarker } from '../util.js';

// app/services/order_service.py -> app.services.order_service
function moduleOf(file) {
  return file.replace(/\.py$/, '').split('/').join('.');
}

function inputDocFields(binding) {
  return binding.input_fields.filter(field => field.name !== 'expected_revision');
}

function adapterFunction(binding) {
  const symbol = binding.symbol || binding.proposed_symbol;
  const lines = [];
  const fields = inputDocFields(binding);
  const args = ['principal=ctx.principal'];
  for (const param of binding.path_params) args.push(`${param}=path_params["${param}"]`);
  // The contract permits optional fields.  Do not index every field eagerly:
  // a normal search such as {"q":"runner"} must not fail because it omits
  // optional pagination/filter arguments.  The generated adapter forwards
  // only fields that were actually validated in the input.
  for (const field of fields) args.push(`**({"${field.name}": input["${field.name}"]} if "${field.name}" in input else {})`);
  if (binding.contract.concurrency_mode === 'resource_revision') args.push('expected_revision=ctx.expected_revision');
  if (binding.contract.idempotency === 'required') args.push('idempotency_key=ctx.action_id');
  const traced = binding.route ? ` (traced from ${binding.route.method} ${binding.route.path})` : '';
  lines.push(`async def ${binding.operation}(ctx, input, path_params):`);
  lines.push(`    # TODO: reconcile this call with ${symbol.name}.${symbol.method}'s real signature${traced}.`);
  lines.push(`    return ${symbol.name}.${symbol.method}(`);
  for (const arg of args) lines.push(`        ${arg},`);
  lines.push(`    )`);
  return lines.join('\n');
}

function adapterFile(resource, bindings) {
  const path = bindings[0].adapter_file;
  const symbols = new Map();
  for (const binding of bindings) {
    const symbol = binding.symbol || binding.proposed_symbol;
    if (symbol) symbols.set(`${symbol.file}#${symbol.name}`, symbol);
  }
  const imports = [...symbols.values()].sort((a, b) => (a.file + a.name < b.file + b.name ? -1 : 1))
    .map(symbol => `from ${moduleOf(symbol.file)} import ${symbol.name}`);
  const body = `"""Merchant-owned Auteric adapters for the "${resource}" resource.

Generated scaffold from the locked contracts registry; review the TODOs, then
own this file. Rerunning \`auteric bind\` never overwrites hand edits.

Conventions:
  - Contract input fields keep their snake_case names (input["product_id"]).
  - Identity comes only from ctx.principal (verified execution token); never
    read a principal from the request body.
"""
${imports.join('\n')}


${bindings.map(adapterFunction).join('\n\n\n')}
`;
  return { path, content: addMarker(path, body), kind: 'adapter', operations: bindings.map(binding => binding.operation) };
}

function extractionPatch(binding) {
  const proposed = binding.proposed_symbol;
  const body = `# Service extraction proposal: ${binding.operation}

The route \`${binding.route?.method} ${binding.route?.path}\` (${binding.route?.file}${binding.route?.line ? `:${binding.route.line}` : ''})
runs its business logic inline, so no business service symbol could be traced.
Nothing was moved automatically — this proposal is for review. Apply it
manually, then rerun \`auteric bind\` so the validator can resolve the import.

## 1. Create \`${proposed.file}\`

\`\`\`python
class ${proposed.name}:
    """Extracted from the inline handler in ${binding.route?.file}:${binding.route?.line}."""

    @staticmethod
    def ${binding.operation}(principal, **kwargs):
        # TODO: move the inline handler body here unchanged.
        raise NotImplementedError
\`\`\`

## 2. Update the controller

\`${binding.route?.file}\` keeps its HTTP concerns and calls
\`${proposed.name}.${proposed.method}\`; the generated adapter
\`${binding.adapter_file}\` calls the same symbol, so the old browser flow and
the new agent flow share one code path.

## 3. Prove the browser flow

Fill in the regression stub at \`${binding.regression_stub}\` and run it before
and after the extraction; the pre-extraction response shape must not change.
`;
  return { path: binding.extraction_patch, content: addMarker(binding.extraction_patch, body), kind: 'patch', operations: [binding.operation] };
}

function regressionStub(binding) {
  const body = `"""Regression stub for the browser flow of ${binding.route?.method} ${binding.route?.path}.

Generated alongside the extraction proposal (${binding.extraction_patch}).
Wire the app fixture and assertions, then remove the skip — this test must
pass BEFORE and AFTER the service extraction lands.
"""
import pytest


@pytest.mark.skip(reason="wire the app fixture")
def test_${binding.operation}_browser_flow_unchanged_after_extraction():
    # 1. Start the merchant app against a test database.
    # 2. Authenticate the way the browser does.
    # 3. Exercise ${binding.route?.method} ${binding.route?.path} and assert the
    #    pre-extraction response status and body shape.
    raise AssertionError("not implemented")
`;
  return { path: binding.regression_stub, content: addMarker(binding.regression_stub, body), kind: 'test', operations: [binding.operation] };
}

function compositionRoot(plan) {
  const path = plan.composition_root;
  const resources = [...new Set(plan.bindings.map(binding => binding.resource))].sort();
  const imports = [];
  for (const resource of resources) {
    const bindings = plan.bindings.filter(binding => binding.resource === resource);
    imports.push(`from auteric_bindings.adapters.${resource} import ${bindings.map(binding => binding.operation).join(', ')}`);
  }
  const adapters = plan.bindings.map(binding => `            "${binding.operation}": ${binding.operation},`);
  const operations = plan.bindings.map(binding => binding.operation).sort();
  const body = `"""Auteric composition root (generated).

Mount the returned app so the Auteric router is reachable at /api/auteric/v1
and /.well-known/ucp is served before any catch-all. See the auteric-merchant
README for the security notes behind this wiring.
"""
from fastapi import FastAPI

from auteric_merchant import (
    InMemoryExecutionStore,
    Installation,
    MapPrincipalResolver,
    MerchantRuntime,
    TrustBundle,
)
from auteric_merchant.fastapi_driver import create_auteric_router
from auteric_merchant.well_known import create_well_known_router

${imports.join('\n')}

OPERATIONS = [${operations.map(operation => `"${operation}"`).join(', ')}]


def create_auteric_app(
    installation: Installation,
    trust: TrustBundle,
    discovery_service=None,
    principal_resolver=None,
) -> FastAPI:
    runtime = MerchantRuntime(
        installation=installation,
        trust=trust,
        adapters={
${adapters.join('\n')}
        },
        # TODO: InMemoryExecutionStore and MapPrincipalResolver are dev-only;
        # back execution_store and principal_resolver with durable stores (SDK
        # README, execution-store section) before going live.
        execution_store=InMemoryExecutionStore(),
        principal_resolver=principal_resolver or MapPrincipalResolver({}),
    )
    app = FastAPI()
    if discovery_service is not None:
        app.include_router(create_well_known_router(discovery_service))
    app.include_router(create_auteric_router(runtime))
    return app
`;
  return { path, content: addMarker(path, body), kind: 'composition', operations };
}

export function generatePython(plan) {
  const artifacts = [
    { path: 'auteric_bindings/__init__.py', content: addMarker('auteric_bindings/__init__.py', '"""Generated Auteric merchant bindings."""\n'), kind: 'package', operations: [] },
    { path: 'auteric_bindings/adapters/__init__.py', content: addMarker('auteric_bindings/adapters/__init__.py', '"""Generated Auteric operation adapters."""\n'), kind: 'package', operations: [] },
  ];
  const byResource = new Map();
  for (const binding of plan.bindings) {
    if (!byResource.has(binding.resource)) byResource.set(binding.resource, []);
    byResource.get(binding.resource).push(binding);
  }
  for (const resource of [...byResource.keys()].sort()) {
    artifacts.push(adapterFile(resource, byResource.get(resource)));
  }
  artifacts.push(compositionRoot(plan));
  for (const binding of plan.bindings) {
    if (binding.action !== 'extract_service') continue;
    artifacts.push(extractionPatch(binding));
    artifacts.push(regressionStub(binding));
  }
  return artifacts;
}
