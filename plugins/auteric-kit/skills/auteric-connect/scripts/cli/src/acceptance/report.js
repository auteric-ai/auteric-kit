// Acceptance evidence report: .auteric/acceptance-report.json. Carries
// per-operation scenario evidence and a digest; carries no secrets, no tokens
// and no source snippets (redaction enforced before writing).
import { createHash } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { redactSecrets } from '../progress.js';
import { REGISTRY_DIGEST, REGISTRY_VERSION } from './contracts.js';

export const REPORT_PATH = join('.auteric', 'acceptance-report.json');

function sha256Hex(data) {
  return createHash('sha256').update(data).digest('hex');
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

// One scenario outcome: {name, outcome: passed|failed|pending, detail, elapsed_ms}.
export function operationRecord(operation, scenarios) {
  const failed = scenarios.filter(scenario => scenario.outcome === 'failed');
  const pending = scenarios.filter(scenario => scenario.outcome === 'pending');
  const verdict = failed.length ? 'failed' : pending.length ? 'pending' : 'verified';
  const reason = failed[0] ? `${failed[0].name}: ${failed[0].detail}` : pending[0] ? `${pending[0].name}: ${pending[0].detail}` : null;
  const record = {
    operation,
    verdict,
    ...(reason ? { reason } : {}),
    scenarios,
  };
  record.evidence_digest = 'sha256:' + sha256Hex(Buffer.from(canonical({ operation, verdict, scenarios }), 'utf8'));
  return record;
}

export function buildReport({ manifest, validation, operations, environment = 'dev', transport = 'native_http' }) {
  const summary = {
    total: operations.length,
    verified: operations.filter(record => record.verdict === 'verified').length,
    failed: operations.filter(record => record.verdict === 'failed').length,
    pending: operations.filter(record => record.verdict === 'pending').length,
  };
  return {
    report: 'auteric-acceptance/v1',
    environment,
    transport,
    registry: { digest: REGISTRY_DIGEST, version: REGISTRY_VERSION },
    sdk: manifest?.sdk || null,
    composition_root: manifest?.composition_root || null,
    static_validation: { ok: validation?.ok ?? null, errors: (validation?.errors || []).map(error => `${error.check}: ${error.message}`) },
    operations: Object.fromEntries(operations.map(record => [record.operation, record])),
    summary,
  };
}

export function writeReport(root, report) {
  // Last-line defense: the serialized report must not contain token or key
  // material even if a scenario detail was built from unexpected input.
  const redacted = redactSecrets(report);
  const path = join(root, REPORT_PATH);
  mkdirSync(dirname(path), { recursive: true });
  const contents = JSON.stringify(redacted, null, 2) + '\n';
  if (/-----BEGIN|eyJ[A-Za-z0-9_-]{8,}\.|Bearer\s/i.test(contents)) throw Error('refusing to write a report containing credential material');
  writeFileSync(path, contents, { mode: 0o644 });
  return path;
}
