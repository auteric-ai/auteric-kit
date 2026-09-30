// Binding planner: consumes an inventory report and produces a deterministic
// binding plan. Per candidate operation it decides the target SDK language
// (from the service-graph backend node), the adapter file path, and the
// strategy action, following the connection preference order (plan §7.3):
//   1. bind_service_call   - direct local service call with explicit caller context
//   2. bind_internal_api   - documented internal API with service credentials
//   3. extract_service     - minimal extraction of the business service from an
//                            existing route (patch proposal + regression stub)
//   4. merchant_decision   - no safe identity/payment path; the precise
//                            decision needed is spelled out, nothing is bound
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadOperationsRegistry } from '../inventory/operations.js';
import { resourceOf, camelCase, pascalCase, pathParams, routeParams, tsType } from './naming.js';
import { candidateFor } from './selection.js';

const STRATEGY_ACTION = {
  local_service_call: 'bind_service_call',
  internal_api: 'bind_internal_api',
  requires_extraction: 'extract_service',
  requires_merchant_decision: 'merchant_decision',
};

// Identity read from the request/session inside a route handler means there
// is no safe principal path for an adapter: the SDK delivers identity as
// ctx.principal from the verified token, so these routes need a merchant
// decision before any binding is generated.
const IDENTITY_PATTERNS = [
  { re: /req\.session\.(user|userId|buyer|customer)\b/, label: 'req.session.user' },
  { re: /req\.user\b/, label: 'req.user' },
  { re: /session\[["'](user|user_id|buyer|customer)["']\]/, label: 'session["user"]' },
  { re: /session\.get\(["'](user|user_id|buyer|customer)["']\)/, label: 'session.get("user")' },
  { re: /\brequest\.user\b/, label: 'request.user' },
  { re: /\bcurrent_user\b/, label: 'current_user' },
  { re: /session\.Get\(["'](user|user_id)["']\)/, label: 'session.Get("user")' },
];

function languageOf(node) {
  const languages = node?.languages || [];
  if (languages.includes('javascript') || languages.includes('typescript')) return 'node';
  if (languages.includes('python')) return 'python';
  if (languages.includes('go')) return 'go';
  return null;
}

function readSource(root, relativePath) {
  try {
    if (relativePath && existsSync(join(root, relativePath))) return readFileSync(join(root, relativePath), 'utf8');
  } catch { /* unreadable file: treated as absent */ }
  return null;
}

function identityCoupling(root, candidate) {
  const entrypoint = candidate.evidence.entrypoints[0];
  const content = readSource(root, entrypoint?.file);
  if (!content) return null;
  return IDENTITY_PATTERNS.find(pattern => pattern.re.test(content)) || null;
}

function sameParameter(left, right) {
  return left === right || camelCase(left) === camelCase(right);
}

// An internal HTTP call is only safe to generate when the *merchant* route
// (not the canonical Auteric route) and every field we will serialize are
// evidenced in source.  The inventory deliberately does not guess aliases
// such as product_id -> sku: those require a merchant decision.
function internalApiProof(root, candidate, record, registryDir) {
  const route = candidate.evidence.entrypoints[0];
  if (!route || route.kind !== 'rest' || !route.path?.startsWith('/')) {
    return { reason: 'internal API binding requires a traced REST route with an absolute path' };
  }
  if (!record.merchant_http?.method || route.method !== record.merchant_http.method) {
    return { reason: `internal API method mismatch: traced ${route.method || 'unknown'} does not match contract ${record.merchant_http?.method || 'unknown'}` };
  }
  const contractParams = pathParams(record.merchant_http.path);
  const merchantParams = routeParams(route.path);
  if (contractParams.length !== merchantParams.length) {
    return { reason: `internal API path parameters cannot be proven: contract has ${contractParams.length}, traced route has ${merchantParams.length}` };
  }
  const mappedParams = {};
  for (const merchantParam of merchantParams) {
    const contractParam = contractParams.find(name => sameParameter(name, merchantParam));
    if (!contractParam || mappedParams[merchantParam]) {
      return { reason: `internal API path parameter ${merchantParam} has no unambiguous contract mapping` };
    }
    mappedParams[merchantParam] = contractParam;
  }

  const fields = inputFields(registryDir, record).filter(field => field.name !== 'expected_revision');
  const source = readSource(root, route.file) || '';
  for (const field of fields) {
    // Exact snake_case or deterministic camelCase spelling is the only
    // automatic mapping accepted.  Anything else remains pending.
    const names = [field.name, camelCase(field.name)];
    if (!names.some(name => new RegExp(`\\b${name}\\b`).test(source))) {
      return { reason: `internal API field mapping cannot be proven for ${field.name} in ${route.file}` };
    }
  }
  const method = route.method;
  return {
    route: { method, path: route.path, path_params: mappedParams },
    input_transport: ['GET', 'DELETE'].includes(method) ? 'query' : 'json_body',
  };
}

function decisionFor(candidate, report) {
  const entrypoint = candidate.evidence.entrypoints[0];
  const at = entrypoint ? `${entrypoint.method} ${entrypoint.path} (${entrypoint.file}${entrypoint.line ? `:${entrypoint.line}` : ''})` : candidate.operation;
  if (report.graph.conflicts.some(item => item.type === 'backend_selection_required')) {
    const backends = report.graph.conflicts.find(item => item.type === 'backend_selection_required').evidence.map(item => item.node).join(', ');
    return `backend selection: multiple authoritative backends detected (${backends}); choose one and rerun with --backend <dir>`;
  }
  if (entrypoint?.kind === 'openapi') {
    return `implementation binding: ${candidate.operation} was derived from an OpenAPI document at ${entrypoint.file}; identify the implementing service before binding`;
  }
  const ambiguous = candidate.reasons.find(reason => reason.startsWith('ambiguous operation match'));
  if (ambiguous) return `operation disambiguation: route ${at} matches ${ambiguous.replace('ambiguous operation match: ', '')}; confirm the intended operation`;
  const boundary = candidate.reasons.find(reason => reason.startsWith('application boundary required'));
  if (boundary) return `${boundary}; traced ${at}`;
  if (candidate.gaps.length) return `manual review for ${candidate.operation} at ${at}: ${candidate.gaps.join('; ')}`;
  return `manual review required for ${candidate.operation} at ${at}`;
}

// Schema refs in registry records are relative to the contracts package root
// (the registry directory's grandparent): "schemas/cart/add_to_cart.input.json".
function inputFields(registryDir, record) {
  const ref = record?.schemas?.input;
  if (!ref) return [];
  try {
    const schema = JSON.parse(readFileSync(join(registryDir, '..', '..', ref), 'utf8'));
    const required = new Set(schema.required || []);
    return Object.keys(schema.properties || {}).sort().map(name => ({
      name,
      required: required.has(name),
      type: tsType(schema.properties[name]),
    }));
  } catch (error) { throw new Error(`Locked contract input schema unavailable for ${record?.operation || record?.schemas?.input}: ${error.message}`); }
}

// Adapter layout per language. Node keeps generated code next to the traced
// routes (their shared top-level directory, e.g. server/); Python and Go use
// root-level packages because their imports are module-path based.
function layoutFor(language, candidates) {
  if (language === 'python') {
    return {
      adapter_file: resource => `auteric_bindings/adapters/${resource}.py`,
      composition_root: 'auteric_bindings/runtime.py',
      extraction_patch: operation => `auteric_bindings/extraction/${operation}.patch.md`,
      regression_stub: operation => `auteric_bindings/tests/test_${operation}_regression.py`,
      proposed_service: resource => ({ file: `services/${resource}_service.py`, name: `${pascalCase(resource)}Service` }),
    };
  }
  if (language === 'go') {
    return {
      adapter_file: resource => `auteric/adapters_${resource}.go`,
      composition_root: 'auteric/runtime.go',
      extraction_patch: operation => `auteric/extraction/${operation}.patch.md`,
      regression_stub: operation => `auteric/${operation}_regression_test.go`,
      proposed_service: resource => ({ file: `internal/${resource}/service.go`, name: `${pascalCase(resource)}Service` }),
    };
  }
  const files = candidates.flatMap(candidate => candidate.evidence.entrypoints.map(entrypoint => entrypoint.file)).filter(Boolean);
  const tops = new Set(files.filter(file => file.includes('/')).map(file => file.split('/')[0]));
  const top = tops.size === 1 ? [...tops][0] : '.';
  const root = top === '.' ? 'auteric' : `${top}/auteric`;
  return {
    adapter_file: resource => `${root}/adapters/${resource}.ts`,
    composition_root: `${root}/index.ts`,
    extraction_patch: operation => `${root}/extraction/${operation}.patch.md`,
    regression_stub: operation => `${root}/tests/${operation}.regression.test.ts`,
    proposed_service: resource => ({ file: `${top === '.' ? 'services' : `${top}/services`}/${resource}Service.ts`, name: `${pascalCase(resource)}Service` }),
  };
}

export function planBindings(report, options = {}) {
  const root = options.root || report.repo;
  const registryDir = options.registryDir || (report.registry?.source === 'contracts' ? report.registry.path : null);
  const registry = loadOperationsRegistry(registryDir || undefined);
  if (!registry) throw Error('Locked contracts registry not found; binding requires packages/commerce-contracts/registry/operations');
  const backendNode = report.graph.nodes.find(node => node.id === (report.graph.verdict.backend || '.'))
    || report.graph.nodes.find(node => node.roles.includes('backend'));
  const language = languageOf(backendNode);
  const layout = layoutFor(language || 'node', report.candidates);

  const bindings = [];
  const decisions = [];
  for (let candidate of report.candidates) {
    const record = registry.operations[candidate.operation];
    if (!record) {
      decisions.push({ operation: candidate.operation, decision: `no locked registry record for ${candidate.operation}; nothing was bound`, evidence: candidate.evidence.entrypoints[0] || null });
      continue;
    }
    if (!language) {
      decisions.push({ operation: candidate.operation, decision: `unsupported backend language (${(backendNode?.languages || []).join(', ') || 'unknown'}); supported binding targets are node, python and go`, evidence: candidate.evidence.entrypoints[0] || null });
      continue;
    }
    let action = STRATEGY_ACTION[candidate.strategy] || 'merchant_decision';
    let decision = null;
    if (action === 'extract_service') {
      const coupling = identityCoupling(root, candidate);
      if (coupling) {
        action = 'merchant_decision';
        const entrypoint = candidate.evidence.entrypoints[0];
        decision = `buyer identity integration: route ${entrypoint.file} reads identity from the request session (${coupling.label}); extraction needed so the adapter receives ctx.principal from the verified token`;
      }
    }
    if (action === 'bind_internal_api') {
      const coupling = identityCoupling(root, candidate);
      if (candidate.evidence.authorization?.type?.split('+').includes('session') || coupling) {
        action = 'merchant_decision';
        const entrypoint = candidate.evidence.entrypoints[0];
        decision = `buyer identity integration: traced internal route ${entrypoint.file} depends on browser session identity${coupling ? ` (${coupling.label})` : ''}; expose a service-to-service route that accepts the verified principal before binding`;
      } else {
        const proof = internalApiProof(root, candidate, record, registry.path);
        if (proof.reason) {
          action = 'merchant_decision';
          decision = `manual review for ${candidate.operation}: ${proof.reason}`;
        } else {
          candidate = { ...candidate, internal_api: proof };
        }
      }
    }
    if (action === 'merchant_decision' && !decision) decision = decisionFor(candidate, report);
    const resource = resourceOf(candidate.operation);
    const entrypoint = candidate.evidence.entrypoints[0];
    if (action === 'merchant_decision') {
      decisions.push({ operation: candidate.operation, decision, evidence: entrypoint || null, gaps: candidate.gaps });
      continue;
    }
    const symbol = candidate.evidence.business_symbol
      ? { name: candidate.evidence.business_symbol.name, method: candidate.evidence.business_symbol.method, file: candidate.evidence.business_symbol.file }
      : null;
    const binding = {
      operation: candidate.operation,
      action,
      resource,
      language,
      adapter_file: layout.adapter_file(resource),
      symbol,
      path_params: pathParams(record.merchant_http?.path),
      input_fields: inputFields(registry.path, record),
      route: entrypoint ? { method: entrypoint.method, path: entrypoint.path, file: entrypoint.file, line: entrypoint.line } : null,
      contract: {
        method: record.merchant_http?.method,
        path: record.merchant_http?.path,
        contract_version: record.contract_version,
        side_effect: record.side_effect,
        concurrency_mode: record.concurrency?.mode || null,
        idempotency: record.idempotency || null,
      },
      source_digest: candidate.evidence.source_digest,
      gaps: candidate.gaps,
    };
    if (action === 'bind_internal_api') binding.internal_api = candidate.internal_api;
    if (action === 'extract_service') {
      const proposed = layout.proposed_service(resource);
      binding.proposed_symbol = { name: proposed.name, method: camelCase(candidate.operation), file: proposed.file };
      binding.extraction_patch = layout.extraction_patch(candidate.operation);
      binding.regression_stub = layout.regression_stub(candidate.operation);
    }
    const candidateId = candidateFor(binding).candidate_id;
    if (options.requireMerchantSelection && !options.approvedCandidateIds?.has(candidateId)) {
      decisions.push({
        operation: candidate.operation,
        decision: `merchant adapter selection: approve candidate ${candidateId} before code generation`,
        evidence: entrypoint || null,
        gaps: candidate.gaps,
        candidate_id: candidateId,
      });
      continue;
    }
    binding.candidate_id = candidateId;
    bindings.push(binding);
  }

  return {
    plan: 'auteric-binding/v1',
    backend: { id: backendNode?.id || '.', language, frameworks: backendNode?.frameworks || [] },
    registry: { path: registry.path },
    composition_root: layout.composition_root,
    bindings: bindings.sort((a, b) => (a.operation < b.operation ? -1 : 1)),
    decisions: decisions.sort((a, b) => (a.operation < b.operation ? -1 : 1)),
    summary: {
      found: report.candidates.length,
      bound: bindings.length,
      merchant_decisions: decisions.length,
    },
  };
}
