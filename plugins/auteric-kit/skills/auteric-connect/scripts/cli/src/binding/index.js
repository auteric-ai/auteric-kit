// Binding pipeline: inventory -> plan -> generate -> reconcile -> manifest ->
// validate. The public surface of src/binding/.
import { resolve } from 'node:path';
import { inventoryRepo } from '../inventory/index.js';
import { planBindings } from './plan.js';
import { generateNode } from './generators/node.js';
import { generatePython } from './generators/python.js';
import { generateGo } from './generators/go.js';
import { buildManifest, readManifest, writeManifestIfChanged, MANIFEST_PATH } from './manifest.js';
import { reconcile } from './reconcile.js';
import { validateInstallation } from './validate.js';

export { planBindings } from './plan.js';
export { generateNode } from './generators/node.js';
export { generatePython } from './generators/python.js';
export { generateGo } from './generators/go.js';
export { buildManifest, readManifest, writeManifestIfChanged, MANIFEST_PATH, sdkPin } from './manifest.js';
export { reconcile, symbolDrift } from './reconcile.js';
export { validateInstallation } from './validate.js';
export { addMarker, splitMarker, markerValid, bindingDigest, sha256Hex } from './util.js';

const GENERATORS = { node: generateNode, python: generatePython, go: generateGo };

export function generate(plan, context = {}) {
  const generator = GENERATORS[plan.backend.language];
  if (!generator) return [];
  return generator(plan, context);
}

export async function bindRepo(root, options = {}) {
  root = resolve(root);
  const report = options.report || await inventoryRepo(root, { backendDir: options.backendDir, registryDir: options.registryDir });
  const plan = planBindings(report, { root, registryDir: options.registryDir });
  const artifacts = generate(plan, { root });
  const previous = readManifest(root);
  const reconciliation = reconcile(root, artifacts, previous, plan);
  const manifest = buildManifest(plan, { root, reconcileActions: reconciliation.actions });
  const manifestWritten = writeManifestIfChanged(root, manifest);
  const validation = validateInstallation(root, { registryDir: options.registryDir });
  const writes = reconciliation.actions.filter(action => ['written', 'regenerated'].includes(action.action)).length + (manifestWritten ? 1 : 0);
  return {
    report,
    plan,
    manifest,
    reconcile: reconciliation,
    validation,
    summary: {
      found: report.candidates.length,
      bound: plan.bindings.length,
      verified: validation.operations.verified,
      merchant_decisions: plan.decisions.length,
      files_written: writes,
      revalidation_required: reconciliation.revalidation_required,
      pending: plan.decisions.length > 0 || !validation.ok,
    },
  };
}

// Human summary in the plan §18 style: counts first, then per-gap and
// per-decision lines. Never claims full integration while anything pends.
export function bindingSummaryLines(result) {
  const { summary, plan, validation } = result;
  const lines = [`Binding: ${summary.found} found, ${summary.bound} bound, ${summary.verified} verified`];
  for (const binding of plan.bindings) {
    for (const gap of binding.gaps || []) lines.push(`Gap (${binding.operation}): ${gap}`);
  }
  for (const decision of plan.decisions) lines.push(`Decision required (${decision.operation}): ${decision.decision}`);
  for (const action of result.reconcile.actions) {
    if (action.action === 'manual_edit_preserved') lines.push(`Preserved hand-edited file: ${action.path}`);
    if (action.action === 'symbol_drift_preserved') lines.push(`Symbol change (${(action.operations || []).join(', ')}): proposed import-update patch at ${action.path.replace(/\.[^.]+$/, '.import-update.patch.md')}; adapter left untouched`);
  }
  for (const error of validation.errors) lines.push(`Validation (${error.check}): ${error.message}`);
  if (summary.pending) {
    lines.push('Integration incomplete: merchant decisions or validation problems are pending; no full integration is claimed.');
  } else {
    lines.push(`All ${summary.bound} bound operation(s) verified. Runtime activation, trust pinning and discovery remain separate steps.`);
  }
  return lines;
}
