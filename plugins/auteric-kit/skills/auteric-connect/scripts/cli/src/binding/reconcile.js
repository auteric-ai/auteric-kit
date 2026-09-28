// Rerun reconciliation. Compares freshly generated artifacts with the tree on
// disk and applies the rerun rules:
//   - unchanged input (stable operation ID + same source digest) produces
//     byte-identical artifacts, so nothing is written;
//   - a generated file whose trailing marker no longer matches its body was
//     hand-edited by the merchant: it is never overwritten and is reported as
//     manual_edit_preserved;
//   - a changed merchant source symbol (addLine -> appendLine) yields an
//     import-update patch proposal plus revalidation_required — the adapter
//     is NOT silently regenerated.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { addMarker, markerValid } from './util.js';

function driftPatch(operation, binding, drift) {
  const path = binding.adapter_file.replace(/\.[^.]+$/, '.import-update.patch.md');
  const body = `# Import-update proposal: ${operation}

The merchant service symbol changed since the last binding run:

- previous: \`${drift.from.name}.${drift.from.method}\` (${drift.from.file})
- current:  \`${drift.to.name}.${drift.to.method}\` (${drift.to.file})

The adapter \`${binding.adapter_file}\` was NOT regenerated — silent rewrites
are never applied. Review and apply by hand:

1. Update the call in \`${binding.adapter_file}\` from
   \`${drift.from.method}\` to \`${drift.to.method}\` and reconcile the
   argument list with the new signature.
2. Delete this proposal file.
3. Rerun \`auteric validate\` (or \`auteric bind\`) — the operation stays
   flagged revalidation_required until the validator is green.
`;
  return { path, content: addMarker(path, body), kind: 'patch', operations: [operation] };
}

// Detects symbol drift per operation: same stable operation ID, but the
// traced business symbol differs from what the previous manifest recorded —
// and the on-disk adapter does not already reference the new symbol. The
// content check makes the loop converge: once the merchant applies the
// proposed update by hand, the next rerun reports no drift.
export function symbolDrift(plan, previous, root) {
  const drifted = new Map();
  for (const binding of plan.bindings) {
    const before = previous?.operations?.[binding.operation];
    if (!before?.symbol || !binding.symbol) continue;
    const changed = before.symbol.name !== binding.symbol.name
      || before.symbol.method !== binding.symbol.method
      || before.symbol.file !== binding.symbol.file;
    if (!changed) continue;
    const adapterPath = join(root, binding.adapter_file);
    const adapter = existsSync(adapterPath) ? readFileSync(adapterPath, 'utf8') : '';
    const method = binding.symbol.method.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (new RegExp(`\\b${method}\\s*\\(`).test(adapter)) continue;
    drifted.set(binding.operation, { from: before.symbol, to: binding.symbol });
  }
  return drifted;
}

export function reconcile(root, artifacts, previous, plan) {
  const actions = [];
  const drifted = symbolDrift(plan, previous, root);
  let revalidationRequired = false;

  const driftPatches = [];
  for (const [operation, drift] of [...drifted.entries()].sort()) {
    const binding = plan.bindings.find(item => item.operation === operation);
    driftPatches.push(driftPatch(operation, binding, drift));
  }

  for (const artifact of [...artifacts, ...driftPatches]) {
    const full = join(root, artifact.path);
    const driftedOps = (artifact.operations || []).filter(operation => drifted.has(operation));
    if (artifact.kind === 'adapter' && driftedOps.length) {
      // Never silently regenerate an adapter whose target symbol moved.
      actions.push({ path: artifact.path, action: 'symbol_drift_preserved', operations: driftedOps, drift: drifted.get(driftedOps[0]) });
      revalidationRequired = true;
      continue;
    }
    const existing = existsSync(full) ? readFileSync(full, 'utf8') : null;
    if (existing === null) {
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(full, artifact.content, { mode: 0o644 });
      actions.push({ path: artifact.path, action: 'written', operations: artifact.operations || [] });
      continue;
    }
    if (existing === artifact.content) {
      actions.push({ path: artifact.path, action: 'unchanged', operations: artifact.operations || [] });
      continue;
    }
    if (markerValid(existing)) {
      // Byte-for-byte generator output from an earlier run: safe to refresh.
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(full, artifact.content, { mode: 0o644 });
      actions.push({ path: artifact.path, action: 'regenerated', operations: artifact.operations || [] });
      continue;
    }
    actions.push({ path: artifact.path, action: 'manual_edit_preserved', operations: artifact.operations || [] });
  }
  return { actions, revalidation_required: revalidationRequired, drift: Object.fromEntries(drifted) };
}
