// Wires the generated zero-dependency contract validators
// (packages/commerce-contracts/generated/ts, synced to contracts.generated.ts)
// into the runtime. Input failures are INVALID_INPUT (§5); output failures mean
// the merchant adapter broke the contract and map to UPSTREAM_ERROR.
import { ContractValidationError, OPERATIONS } from "./contracts.generated.js";
import { MerchantError } from "./errors.js";
export function getOperationContract(operation) {
    const entry = OPERATIONS[operation];
    if (!entry)
        return null;
    return {
        contractVersion: entry.contractVersion,
        method: entry.method,
        path: entry.path,
        sideEffect: entry.sideEffect,
        idempotency: entry.idempotency,
        identity: entry.identity,
    };
}
export function validateOperationInput(operation, input) {
    const entry = OPERATIONS[operation];
    if (!entry)
        throw new MerchantError("RESOURCE_NOT_FOUND", "unknown operation");
    try {
        entry.validateInput(input);
    }
    catch (err) {
        if (err instanceof ContractValidationError) {
            throw new MerchantError("INVALID_INPUT", "request failed contract validation", {
                category: err.category,
                path: err.path,
            });
        }
        throw err;
    }
}
export function validateOperationOutput(operation, output) {
    const entry = OPERATIONS[operation];
    if (!entry)
        throw new MerchantError("RESOURCE_NOT_FOUND", "unknown operation");
    try {
        entry.validateOutput(output);
    }
    catch (err) {
        if (err instanceof ContractValidationError) {
            throw new MerchantError("UPSTREAM_ERROR", "adapter response failed contract validation", {
                category: err.category,
                path: err.path,
            });
        }
        throw err;
    }
}
