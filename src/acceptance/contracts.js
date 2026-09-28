// Locked-contract validators for the acceptance runner. Loads the synced
// generated validators (see scripts/sync-contracts.mjs) and maps operation
// names to their output validators. The runner trusts these — never the
// binding generator's claims about what an adapter returns.
import * as generated from './contracts.generated.js';

export const REGISTRY_DIGEST = generated.REGISTRY_DIGEST;
export const REGISTRY_VERSION = generated.REGISTRY_VERSION;
export const ContractValidationError = generated.ContractValidationError;

function pascalCase(name) {
  return String(name).replace(/(^|_)([a-z0-9])/g, (_, __, ch) => ch.toUpperCase());
}

export function outputValidator(operation) {
  const validator = generated[`validate${pascalCase(operation)}Output`];
  return typeof validator === 'function' ? validator : null;
}

// Throws ContractValidationError when the success payload breaks the locked
// output schema; returns a short description otherwise.
export function validateOperationOutput(operation, value) {
  const validator = outputValidator(operation);
  if (!validator) throw new ContractValidationError('unknown_operation', '$', `no locked output validator for ${operation}`);
  validator(value, '$');
}

// MEP/1 §5 envelope check. The locked Error schema requires action_id, which
// the merchant SDK only attaches once a token was verified; failures raised
// before that point (for example an idempotency conflict at the ledger) carry
// no action_id yet. Those envelopes are validated field-by-field and the gap
// is recorded in the returned notes instead of being silently waived.
export function validateErrorEnvelope(value) {
  const notes = [];
  try {
    generated.validateError(value, '$');
  } catch (error) {
    if (error instanceof ContractValidationError && error.category === 'missing_required' && error.path === '$.error.action_id') {
      const { action_id, ...rest } = value.error;
      generated.validateError({ error: { ...rest, action_id: 'action_unchecked0' } }, '$');
      notes.push('envelope has no action_id (error raised before handler execution)');
    } else {
      throw error;
    }
  }
  return notes;
}
