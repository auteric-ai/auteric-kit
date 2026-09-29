import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const ADAPTER_CANDIDATES_PATH = join('.auteric', 'adapter-candidates.json');
export const ADAPTER_SELECTION_PATH = join('.auteric', 'adapter-selection.json');
export const STORE_PLATFORMS = Object.freeze(['shopify', 'wix', 'woocommerce', 'custom']);

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return 'sha256:' + createHash('sha256').update(canonical(value)).digest('hex');
}

export function normalizeStorePlatform(value = 'custom') {
  const platform = String(value).trim().toLowerCase();
  if (!STORE_PLATFORMS.includes(platform)) {
    throw Error(`Unsupported store platform ${value}; choose ${STORE_PLATFORMS.join(', ')}`);
  }
  return platform;
}

export function platformAdapter(platform) {
  platform = normalizeStorePlatform(platform);
  if (platform === 'custom') {
    return { platform, adapter_family: 'merchant_runtime', onboarding: 'adapter_selection' };
  }
  return { platform, adapter_family: `${platform}_connector`, onboarding: 'oauth_or_platform_credentials' };
}

export function candidateFor(binding) {
  const identity = {
    operation: binding.operation,
    mode: binding.action,
    language: binding.language,
    route: binding.route || null,
    symbol: binding.symbol || binding.proposed_symbol || null,
    source_digest: binding.source_digest,
    contract: binding.contract,
  };
  return {
    candidate_id: digest(identity),
    ...identity,
    adapter_file: binding.adapter_file,
    gaps: binding.gaps || [],
  };
}

export function buildCandidateSet(plan, options = {}) {
  const platform = normalizeStorePlatform(options.platform || 'custom');
  const candidates = plan.bindings.map(candidateFor).sort((a, b) => a.operation.localeCompare(b.operation));
  const body = {
    schema: 'auteric-adapter-candidates/v1',
    platform,
    adapter: platformAdapter(platform),
    backend: plan.backend,
    registry: plan.registry,
    candidates,
  };
  return { ...body, candidate_set_digest: digest(body) };
}

function readJson(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')); } catch { return null; }
}

export function readAdapterSelection(root) {
  return readJson(join(root, ADAPTER_SELECTION_PATH));
}

export function validateAdapterSelection(selection, candidateSet) {
  const errors = [];
  if (!selection || selection.schema !== 'auteric-adapter-selection/v1') errors.push('adapter selection schema is missing or unsupported');
  if (selection?.platform !== candidateSet.platform) errors.push('adapter selection platform does not match the current store choice');
  if (selection?.candidate_set_digest !== candidateSet.candidate_set_digest) errors.push('adapter selection is stale because the candidates changed');
  if (!selection?.approved_by || typeof selection.approved_by !== 'string') errors.push('adapter selection must record approved_by');
  if (!Array.isArray(selection?.approved_candidate_ids)) errors.push('approved_candidate_ids must be an array');
  const known = new Set(candidateSet.candidates.map(item => item.candidate_id));
  const approved = new Set(selection?.approved_candidate_ids || []);
  for (const id of approved) if (!known.has(id)) errors.push(`unknown approved candidate ${id}`);
  if (approved.size !== (selection?.approved_candidate_ids || []).length) errors.push('approved_candidate_ids contains duplicates');
  return { ok: errors.length === 0, errors, approved };
}

export function approveCandidateSet(candidateSet, approvedBy) {
  if (!approvedBy || !String(approvedBy).trim()) throw Error('approvedBy is required');
  return {
    schema: 'auteric-adapter-selection/v1',
    platform: candidateSet.platform,
    candidate_set_digest: candidateSet.candidate_set_digest,
    approved_candidate_ids: candidateSet.candidates.map(item => item.candidate_id),
    approved_by: String(approvedBy).trim(),
    approved_at: new Date().toISOString(),
  };
}

export function writeJsonIfChanged(root, relativePath, value, mode = 0o644) {
  const path = join(root, relativePath);
  const contents = JSON.stringify(value, null, 2) + '\n';
  if (existsSync(path) && readFileSync(path, 'utf8') === contents) return false;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, contents, { mode });
  return true;
}
