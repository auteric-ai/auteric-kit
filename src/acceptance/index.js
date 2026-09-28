// Contract acceptance runner — public surface.
//   import { runAcceptance } from './acceptance/index.js';
//   const { report, summary } = await runAcceptance('/path/to/bound/project');
export { runAcceptance, installNodeRuntime } from './runner.js';
export { buildScenarioPlan, loadVectors } from './scenarios.js';
export { buildReport, writeReport, operationRecord, REPORT_PATH } from './report.js';
export { validateOperationOutput, validateErrorEnvelope, REGISTRY_DIGEST } from './contracts.js';
export { generateGatewayKeys, makeInstallation, makeTrust, DevSigner, installationBindingDigest } from './signer.js';
export { requestHash, canonicalPath, canonicalQuery, RequestHashError } from './request-hash.js';

// Plan §18-style summary for the CLI. Verdicts are per operation; nothing
// claims full acceptance while any operation is not verified.
export function acceptanceSummaryLines({ report, path }) {
  const { summary } = report;
  const lines = [`Acceptance: ${summary.verified}/${summary.total} operations verified over real HTTP (environment=dev)${summary.failed ? `, ${summary.failed} failed` : ''}${summary.pending ? `, ${summary.pending} pending` : ''}`];
  for (const record of Object.values(report.operations)) {
    const scenarios = record.scenarios || [];
    const passed = scenarios.filter(scenario => scenario.outcome === 'passed').length;
    if (record.verdict === 'verified') {
      lines.push(`${record.operation}: verified (${passed}/${scenarios.length} scenarios passed)`);
    } else {
      lines.push(`${record.operation}: ${record.verdict} — ${record.reason || 'no detail'}`);
    }
  }
  if (summary.failed || summary.pending) {
    lines.push('Acceptance incomplete: operations listed above are not verified; no runtime acceptance is claimed.');
  }
  lines.push(`Report: ${path}`);
  return lines;
}
