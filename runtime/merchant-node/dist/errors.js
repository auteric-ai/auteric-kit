// MEP/1 §5 wire error taxonomy. Exactly 14 codes; no stack traces, credentials,
// or payment-provider internals ever reach `message`/`details`.
export const WIRE_ERROR_CODES = {
    INVALID_INPUT: { status: 400, retryable: false },
    UNAUTHENTICATED: { status: 401, retryable: false },
    FORBIDDEN: { status: 403, retryable: false },
    RESOURCE_NOT_FOUND: { status: 404, retryable: false },
    REVISION_CONFLICT: { status: 409, retryable: true },
    OUT_OF_STOCK: { status: 409, retryable: false },
    IDEMPOTENCY_CONFLICT: { status: 409, retryable: false },
    CAPABILITY_DISABLED: { status: 403, retryable: false },
    CONTRACT_MISMATCH: { status: 421, retryable: false },
    RATE_LIMITED: { status: 429, retryable: true },
    EXECUTION_UNCERTAIN: { status: 504, retryable: false },
    PAYMENT_PENDING: { status: 202, retryable: false },
    UPSTREAM_ERROR: { status: 502, retryable: true },
    SSRF_BLOCKED: { status: 502, retryable: false },
};
export class MerchantError extends Error {
    code;
    details;
    constructor(code, message, details) {
        super(message);
        this.name = "MerchantError";
        this.code = code;
        this.details = details;
    }
    get httpStatus() {
        return WIRE_ERROR_CODES[this.code].status;
    }
    get retryable() {
        return WIRE_ERROR_CODES[this.code].retryable;
    }
}
export function isMerchantError(err) {
    return err instanceof MerchantError;
}
/**
 * Normalize any thrown value to the MEP/1 §5 wire envelope. Unknown errors are
 * reported as a generic UPSTREAM_ERROR; the internal message is never leaked.
 */
export function toWireError(err, actionId) {
    const me = isMerchantError(err)
        ? err
        : new MerchantError("UPSTREAM_ERROR", "Upstream error");
    const body = {
        error: {
            code: me.code,
            message: me.message,
            retryable: me.retryable,
        },
    };
    const action = actionId ?? me.details?.action_id;
    if (action)
        body.error.action_id = action;
    if (me.details && Object.keys(me.details).length > 0) {
        const { action_id: _drop, ...rest } = me.details;
        if (Object.keys(rest).length > 0)
            body.error.details = rest;
    }
    return { status: me.httpStatus, body };
}
