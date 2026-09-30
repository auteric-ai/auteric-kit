import { type KeyObject } from "node:crypto";
import type { Clock, InstallationManifest, TrustBundle } from "./types.js";
export declare const CLOCK_SKEW_SECONDS = 5;
export declare const MAX_TOKEN_WINDOW_SECONDS = 30;
export declare const NONCE_TTL_SECONDS = 60;
export declare const MAX_TOKEN_BYTES: number;
export interface ExecutionClaims {
    iss: string;
    aud: string;
    sub: string;
    store_id: string;
    environment: string;
    operation: string;
    contract_version: string;
    binding_digest: string;
    action_id: string;
    jti: string;
    request_hash: string;
    iat: number;
    exp: number;
}
/**
 * Pluggable jti replay cache. Production deployments MUST back this with a
 * shared durable store (nonce protection is meaningless per-instance).
 */
export interface NonceCache {
    /**
     * Atomically claim `jti` for `ttlSeconds`. Returns true when the jti was
     * newly claimed, false when it was already present (replay).
     */
    claim(jti: string, ttlSeconds: number): Promise<boolean>;
}
/**
 * TESTS/DEV ONLY. Process-local nonce cache; offers no protection across
 * instances or restarts. Lazy expiry on access — deliberately uses no timers
 * so the core stays serverless-safe.
 */
export declare class InMemoryNonceCache implements NonceCache {
    private readonly seen;
    private readonly clock;
    constructor(clock?: Clock);
    claim(jti: string, ttlSeconds: number): Promise<boolean>;
}
export declare function importEd25519PublicKey(encoded: string): KeyObject;
export interface VerifyExecutionTokenOptions {
    trust: TrustBundle;
    installation: InstallationManifest;
    /** The manifest route the request matched (operation + method + path template). */
    route: {
        operation: string;
        method: string;
    };
    /** Hex SHA-256 computed over the raw request per MEP/1 §3. */
    computedRequestHash: string;
    nonceCache: NonceCache;
    /**
     * Explicitly opt in to the two MEP/1 operational probes.  These probes are
     * still fully signed and bound to the installation, but their subject is a
     * gateway service principal rather than a buyer/guest pairwise subject.
     * Never enable this for commerce operation routes.
     */
    allowGatewayProbeSubject?: boolean;
    clock?: Clock;
}
/**
 * Verify a signed execution JWT end to end. Throws MerchantError with the
 * §5 wire code on any failure; returns the validated claims on success.
 */
export declare function verifyExecutionToken(token: string, opts: VerifyExecutionTokenOptions): Promise<ExecutionClaims>;
