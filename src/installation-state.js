// The install state is deliberately narrower than individual workflow phases.
// It is safe to persist in a local report and to show to an operator: none of
// these values claims that a generated UCP or a successful build is runtime
// protection.
export const INSTALLATION_STATES = Object.freeze([
  'implementation_required', 'prepared', 'deployment_pending',
  'deployed_unverified', 'verified', 'failed',
]);

const TRANSIENT = new Set(['not_started', 'running']);

export function isInstallationState(value) {
  return INSTALLATION_STATES.includes(value);
}

export function exitCodeForInstallation(state) {
  // A CLI process succeeding means the requested local work completed.  A
  // deployment or runtime verification is a separate, explicitly reported
  // boundary, therefore every non-verified terminal result is non-zero.
  return state === 'verified' ? 0 : state === 'failed' ? 1 : 2;
}

export function normalizeInstallationReport(previous = {}, update = {}) {
  const value = { ...previous, ...update };
  // `status` predates this model and is also used for short-lived per-phase
  // values such as `assistant_running`.  Keep that compatibility field while
  // making the installation claim explicit and machine-readable.
  const status = value.installation_status ?? (isInstallationState(value.status) ? value.status : null);
  if (status === null) return value;
  if (!isInstallationState(status) && !TRANSIENT.has(status)) {
    throw Error(`Unknown Auteric installation status: ${status}`);
  }
  if (status === 'failed' && value.installation_status && (!value.failure_code || !value.message || !value.next_action)) {
    throw Error('A failed installation report requires failure_code, message, and next_action');
  }
  if (status === 'verified' && (!Array.isArray(value.verified_operations) || !value.verified_operations.length)) {
    throw Error('A verified installation report requires verified_operations from runtime evidence');
  }
  if (status === 'deployment_pending' && (!Array.isArray(value.deployable_artifacts) || !value.deployable_artifacts.length)) {
    throw Error('deployment_pending requires Git-tracked deployable_artifacts, not .auteric session files');
  }
  return value;
}
