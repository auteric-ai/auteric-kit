export declare const WIRE_ERROR_CODES: {
    readonly INVALID_INPUT: {
        readonly status: 400;
        readonly retryable: false;
    };
    readonly UNAUTHENTICATED: {
        readonly status: 401;
        readonly retryable: false;
    };
    readonly FORBIDDEN: {
        readonly status: 403;
        readonly retryable: false;
    };
    readonly RESOURCE_NOT_FOUND: {
        readonly status: 404;
        readonly retryable: false;
    };
    readonly REVISION_CONFLICT: {
        readonly status: 409;
        readonly retryable: true;
    };
    readonly OUT_OF_STOCK: {
        readonly status: 409;
        readonly retryable: false;
    };
    readonly IDEMPOTENCY_CONFLICT: {
        readonly status: 409;
        readonly retryable: false;
    };
    readonly CAPABILITY_DISABLED: {
        readonly status: 403;
        readonly retryable: false;
    };
    readonly CONTRACT_MISMATCH: {
        readonly status: 421;
        readonly retryable: false;
    };
    readonly RATE_LIMITED: {
        readonly status: 429;
        readonly retryable: true;
    };
    readonly EXECUTION_UNCERTAIN: {
        readonly status: 504;
        readonly retryable: false;
    };
    readonly PAYMENT_PENDING: {
        readonly status: 202;
        readonly retryable: false;
    };
    readonly UPSTREAM_ERROR: {
        readonly status: 502;
        readonly retryable: true;
    };
    readonly SSRF_BLOCKED: {
        readonly status: 502;
        readonly retryable: false;
    };
};
export type WireErrorCode = keyof typeof WIRE_ERROR_CODES;
export interface WireErrorBody {
    error: {
        code: WireErrorCode;
        message: string;
        retryable: boolean;
        action_id?: string;
        details?: Record<string, unknown>;
    };
}
export interface WireError {
    status: number;
    body: WireErrorBody;
}
export declare class MerchantError extends Error {
    readonly code: WireErrorCode;
    readonly details?: Record<string, unknown>;
    constructor(code: WireErrorCode, message: string, details?: Record<string, unknown>);
    get httpStatus(): number;
    get retryable(): boolean;
}
export declare function isMerchantError(err: unknown): err is MerchantError;
/**
 * Normalize any thrown value to the MEP/1 §5 wire envelope. Unknown errors are
 * reported as a generic UPSTREAM_ERROR; the internal message is never leaked.
 */
export declare function toWireError(err: unknown, actionId?: string): WireError;
