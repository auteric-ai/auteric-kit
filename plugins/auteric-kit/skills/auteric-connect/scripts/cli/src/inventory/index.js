// Inventory engine entrypoint. Usage:
//   import { inventoryRepo } from './inventory/index.js';
//   const report = await inventoryRepo('/path/to/repo', { backendDir, registryDir, budget });
import { resolve } from 'node:path';
import { walkRepo } from './walk.js';
import { DRIVERS } from './drivers/index.js';
import { buildServiceGraph } from './serviceGraph.js';
import { matchCandidates } from './candidates.js';
import { buildReport, humanSummary } from './report.js';
import { loadOperationsRegistry, OPERATION_SIGNALS } from './operations.js';

export { walkRepo, buildServiceGraph, matchCandidates, buildReport, humanSummary, loadOperationsRegistry, OPERATION_SIGNALS, DRIVERS };

export async function inventoryRepo(root, options = {}) {
  const ctx = walkRepo(resolve(root), options.budget);
  const findings = [];
  for (const driver of DRIVERS) {
    const evidence = driver.detect(ctx);
    if (!evidence.length) continue;
    findings.push({ driver: driver.id, evidence, ...driver.analyze(evidence, ctx) });
  }
  const graph = buildServiceGraph(ctx, findings, options);
  const registry = loadOperationsRegistry(options.registryDir);
  const { candidates, needReview } = matchCandidates(ctx, graph, registry);
  return buildReport({ ctx, graph, candidates, needReview, registry, findings });
}
