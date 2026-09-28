// Candidate matcher: maps discovered routes/services to registry operations.
// Matching is semantic, not name fuzzing alone: HTTP method compatibility,
// path-token groups, business-symbol tokens and the registry side_effect all
// have to agree; ambiguity and missing evidence land in need_review with
// reasons. Finding the string "/api/cart" alone never produces a candidate.
import { createHash } from 'node:crypto';
import { OPERATION_SIGNALS } from './operations.js';
import { tokens } from './util.js';

const OUTBOUND_HTTP = /\b(fetch|axios\.[a-z]+|axios\(|requests\.(get|post|put|patch|delete)|http\.Client|http\.Get|http\.Post)\b/;

function routeTokens(route) {
  const values = [route.path, route.handler, route.symbol];
  for (const call of route.serviceCalls || []) values.push(call.symbol, call.method);
  return new Set(values.flatMap(value => tokens(value || '')));
}

function matchOperation(route, tokenSet) {
  const matches = [];
  for (const signal of OPERATION_SIGNALS) {
    if (route.method !== 'ANY' && !signal.methods.includes(route.method)) continue;
    const hit = signal.groups.every(group => group.some(token => tokenSet.has(token)));
    if (!hit) continue;
    if (signal.exclude?.some(token => tokenSet.has(token))) continue;
    matches.push(signal);
  }
  return matches;
}

function digest(ctx, path) {
  const file = ctx.files.find(candidate => candidate.path === path);
  return file ? createHash('sha256').update(file.content).digest('hex') : null;
}

function relatedTests(ctx, tokenSet) {
  const resources = ['cart', 'order', 'checkout', 'product', 'shipping', 'discount'];
  const wanted = resources.filter(resource => tokenSet.has(resource));
  if (!wanted.length) return [];
  return ctx.files
    .filter(file => /(^|\/)(tests?|spec)\//.test(file.path) || /\.(test|spec)\.[^/]+$/.test(file.path) || /(^|\/)test_[^/]*\.py$/.test(file.path))
    .filter(file => wanted.some(resource => file.path.toLowerCase().includes(resource)))
    .map(file => file.path)
    .sort();
}

function routeAuthorization(route, node) {
  let entries = [...(route.auth || [])];
  if (!entries.length) entries = (node?.auth || []).filter(entry => entry.file === route.file);
  if (!entries.length) entries = [...(node?.auth || [])];
  if (!entries.length) return { type: 'none', evidence: [] };
  const types = [...new Set(entries.map(entry => entry.type))];
  return { type: types.length === 1 ? types[0] : types.sort().join('+'), evidence: entries.slice(0, 5) };
}

function routePersistence(route, node, ctx) {
  const files = new Set([route.file]);
  for (const call of route.serviceCalls || []) if (call.resolved_file) files.add(call.resolved_file);
  const entries = (node?.persistence || []).filter(entry => files.has(entry.file));
  if (!entries.length) {
    for (const path of files) {
      const file = ctx.files.find(candidate => candidate.path === path);
      if (!file) continue;
      for (const match of file.content.matchAll(/from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]|^\s*(?:from|import)\s+([\w.]+)/gm)) {
        const specifier = match[1] || match[2] || match[3];
        if (/^(pg|mysql2?|better-sqlite3|sqlite3|@prisma\/client|prisma|mongoose|mongodb|typeorm|sequelize|knex|sqlalchemy|psycopg2?|asyncpg|redis|ioredis|database\/sql|gorm|django\.db)/.test(specifier)) {
          entries.push({ type: specifier.replace(/^@prisma\/client/, 'prisma'), file: path, line: 1, detail: `imports ${specifier}` });
        }
      }
    }
  }
  if (!entries.length) return { type: 'unknown', evidence: [] };
  return { type: entries[0].type, evidence: entries.slice(0, 5) };
}

function routeDependencies(ctx, route) {
  const deps = new Set();
  const file = ctx.files.find(candidate => candidate.path === route.file);
  if (!file) return [];
  for (const match of file.content.matchAll(/from\s+['"]([^'".][^'"]*)['"]|require\(\s*['"]([^'".][^'"]*)['"]\s*\)/g)) {
    deps.add((match[1] || match[2]).split('/').slice(0, match[1]?.startsWith('@') ? 2 : 1).join('/'));
  }
  return [...deps].sort();
}

// Deduplication key: the underlying business symbol when traced, otherwise
// the operation alone (REST + GraphQL on one service must merge into one
// capability, and two unrelated routes to the same operation must not
// multiply candidates when no symbol distinguishes them).
function dedupeKey(route, operation) {
  const call = (route.serviceCalls || [])[0];
  if (call?.resolved_file) return `${operation}::${call.resolved_file}#${call.symbol}.${call.method}`;
  return `${operation}::${route.file}`;
}

export function matchCandidates(ctx, graph, registry) {
  const conflict = graph.conflicts.some(item => item.type === 'backend_selection_required') || graph.verdict.status === 'backend_selection_required';
  const backendIds = new Set(graph.nodes.filter(node => node.roles.includes('backend')).map(node => node.id));
  const nodes = graph.nodes.filter(node => backendIds.has(node.id));
  const byKey = new Map();
  const needReview = [];

  for (const node of nodes) {
    for (const route of node.routes) {
      const tokenSet = routeTokens(route);
      const matches = matchOperation(route, tokenSet);
      if (!matches.length) continue;
      const ambiguous = matches.length > 1;
      const signal = matches[0];
      const key = dedupeKey(route, signal.operation);
      const entrypoint = {
        kind: route.kind,
        method: route.method,
        path: route.path,
        file: route.file,
        line: route.line,
        ...(route.symbol ? { symbol: route.symbol } : {}),
        ...(route.mount ? { mount: route.mount } : {}),
        ...(route.yaml_heuristic ? { yaml_heuristic: true } : {}),
      };
      if (byKey.has(key)) {
        const existing = byKey.get(key);
        const known = existing.evidence.entrypoints.some(item => item.kind === entrypoint.kind && item.method === entrypoint.method && item.path === entrypoint.path && item.file === entrypoint.file);
        if (!known) existing.evidence.entrypoints.push(entrypoint);
        continue;
      }
      const call = (route.serviceCalls || [])[0] || null;
      const authorization = routeAuthorization(route, node);
      const persistence = routePersistence(route, node, ctx);
      const gaps = [];
      const reasons = [];
      if (authorization.type === 'none' || authorization.type === 'unknown') gaps.push('authorization boundary not evidenced at the route (no session/JWT/middleware found)');
      if (persistence.type === 'unknown') gaps.push('persistence not evidenced (no ORM/database client traced)');
      if (!call) gaps.push('no business service symbol traced from the entrypoint');
      if (route.kind === 'openapi') gaps.push('derived from an OpenAPI document; no implementation traced');
      if (ambiguous) reasons.push(`ambiguous operation match: ${matches.map(item => item.operation).join(', ')}`);
      let strategy;
      const routeContent = ctx.files.find(file => file.path === route.file)?.content || '';
      const serviceContent = call?.resolved_file ? ctx.files.find(file => file.path === call.resolved_file)?.content || '' : '';
      if (conflict) strategy = 'requires_merchant_decision';
      else if (route.kind === 'openapi') strategy = 'requires_merchant_decision';
      else if (call?.resolved_file && !OUTBOUND_HTTP.test(serviceContent)) strategy = 'local_service_call';
      else if (OUTBOUND_HTTP.test(routeContent) || OUTBOUND_HTTP.test(serviceContent)) strategy = 'internal_api';
      else strategy = 'requires_extraction';
      if (ambiguous && strategy === 'local_service_call') strategy = 'requires_merchant_decision';
      const contract = registry?.operations?.[signal.operation];
      const candidate = {
        operation: signal.operation,
        status: gaps.length || ambiguous ? 'need_review' : 'candidate',
        strategy,
        evidence: {
          entrypoints: [entrypoint],
          business_symbol: call ? { name: call.symbol, method: call.method, file: call.resolved_file || call.file, line: call.line } : null,
          authorization,
          persistence,
          side_effects: [signal.side],
          dependencies: routeDependencies(ctx, route),
          related_tests: relatedTests(ctx, tokenSet),
          source_digest: digest(ctx, route.file),
        },
        ...(contract ? { contract: { side_effect: contract.side_effect, merchant_http: contract.merchant_http, contract_version: contract.contract_version } } : {}),
        gaps,
        reasons,
      };
      byKey.set(key, candidate);
    }
  }

  const candidates = [...byKey.values()].map(candidate => ({
    ...candidate,
    evidence: {
      ...candidate.evidence,
      entrypoints: candidate.evidence.entrypoints.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : a.kind < b.kind ? -1 : 1)),
    },
  })).sort((a, b) => (a.operation < b.operation ? -1 : a.operation > b.operation ? 1 : 0));

  for (const candidate of candidates) {
    if (candidate.status !== 'need_review') continue;
    needReview.push({
      operation: candidate.operation,
      entrypoint: candidate.evidence.entrypoints[0],
      reasons: [...candidate.reasons, ...candidate.gaps],
    });
  }
  return { candidates, needReview };
}
