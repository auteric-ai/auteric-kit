// Inventory report: structured JSON plus a human summary. Counts distinguish
// endpoints found ≠ commerce candidates ≠ deduped capabilities ≠ tools
// (inventory never creates tools; exposure happens later in the workflow).
const UNSUPPORTED_WRAPPERS = /^(shopify|@shopify\/|@medusajs\/|medusa|@bigcommerce\/|bigcommerce|woocommerce|@woocommerce\/|commercetools|@commercetools\/|saleor|magento|@adobe\/commerce|swell|snipcart)/;

export function unsupportedWrappers(graph) {
  const found = [];
  for (const node of graph.nodes) {
    for (const dependency of node.dependencies) {
      if (UNSUPPORTED_WRAPPERS.test(dependency)) {
        found.push({
          dependency,
          node: node.id,
          status: 'unsupported_wrapper',
          detail: `Commerce platform dependency "${dependency}" detected. Wrapping a hosted platform is not supported by inventory; no adapter or mock was fabricated.`,
        });
      }
    }
  }
  return found.sort((a, b) => (a.dependency < b.dependency ? -1 : 1));
}

export function buildReport({ ctx, graph, candidates, needReview, registry, findings }) {
  const wrappers = unsupportedWrappers(graph);
  const endpoints = graph.nodes.reduce((count, node) => count + node.routes.length, 0);
  const dedupedCapabilities = new Set(candidates.map(candidate => candidate.operation)).size;
  const summary = {
    endpoints_found: endpoints,
    commerce_candidates: candidates.length,
    capabilities_deduped: dedupedCapabilities,
    tools_exposed: 0,
    need_review: needReview.length,
    unsupported_wrappers: wrappers.length,
  };
  const human = [
    `Verdict: ${graph.verdict.status}${graph.verdict.backend ? ` (backend: ${graph.verdict.backend})` : ''}`,
    `Endpoints found: ${summary.endpoints_found} | commerce candidates: ${summary.commerce_candidates} | deduped capabilities: ${summary.capabilities_deduped} | tools exposed: ${summary.tools_exposed}`,
    `Files read: ${ctx.stats.files_read}/${ctx.stats.files_considered} (${ctx.stats.bytes_read} bytes)`,
    ...(needReview.length ? [`Need review: ${needReview.map(item => item.operation).join(', ')}`] : []),
    ...(wrappers.length ? [`Unsupported wrappers: ${wrappers.map(item => item.dependency).join(', ')}`] : []),
    ...graph.gaps.map(gap => `Gap: ${gap}`),
  ];
  return {
    report: 'auteric-inventory/v1',
    repo: ctx.root,
    registry: registry ? { source: 'contracts', path: registry.path, operations: Object.keys(registry.operations).length } : { source: 'embedded', note: 'packages/commerce-contracts registry not found; embedded operation signals used' },
    budget: ctx.stats,
    graph,
    drivers: findings.map(finding => finding.driver).sort(),
    summary,
    candidates,
    need_review: needReview,
    unsupported_wrappers: wrappers,
    gaps: graph.gaps,
    human,
  };
}

export function humanSummary(report) {
  return report.human.join('\n');
}
