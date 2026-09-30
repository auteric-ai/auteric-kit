export interface RequestLike {
    /** HTTP method as received (any case; canonicalized to uppercase). */
    method: string;
    /**
     * Raw request-target path exactly as received on the wire, WITHOUT the query
     * string and before any framework normalization (no decoding, no trailing-slash
     * stripping, no prefix removal). Drivers must capture this before routing
     * middleware rewrites anything. Behind a reverse proxy mount, the configured
     * `trustedProxyPrefix` on the installation is stripped during canonicalization —
     * it is never taken from request data.
     */
    rawPath: string;
    /** Raw query string without the leading '?'. Empty string when absent. */
    rawQuery: string;
    /** Lowercased header names. */
    headers: Record<string, string | string[] | undefined>;
    /** Raw entity body bytes (after transfer decoding, before any parsing). */
    body: Buffer;
}
export interface ResponseLike {
    status: number;
    headers: Record<string, string>;
    body: string;
}
export type Environment = "production" | "staging" | "sandbox" | "dev";
/**
 * Installation manifest: the local, pinned record of what this store serves.
 * Only operations listed here are routed; everything else is denied.
 */
export interface InstallationManifest {
    installationId: string;
    storeId: string;
    environment: Environment;
    enabled: boolean;
    /** Canonical operation names from the registry (subset of the 20 locked ops). */
    operations: string[];
    /** Registered adapter+dependencies digest; mismatch -> CONTRACT_MISMATCH. */
    bindingDigest: string;
    /** Installation-configured reverse-proxy mount prefix (root_path/script_name). */
    trustedProxyPrefix?: string | null;
    /** Max request body bytes; default 1 MiB (MEP/1 §2.2 step 1). */
    maxBodyBytes?: number;
}
/**
 * Pinned trust bundle. Public keys arrive via installation config, never from
 * a token header (jku/x5u are always rejected).
 *
 * Key encodings accepted for each kid entry:
 *   - raw 32-byte Ed25519 key as base64url/base64/hex
 *   - "pem:<SPKI PEM>"
 *   - "spki:<base64 DER SPKI>"
 */
export interface TrustBundle {
    /** Allowlisted gateway issuer URLs (HTTPS), e.g. "https://gateway.example.invalid". */
    issuers: string[];
    /** kid -> Ed25519 public key for execution JWTs. */
    keys: Record<string, string>;
    /** kid -> Ed25519 public key for discovery documents (separate from execution keys). */
    discoveryKeys?: Record<string, string>;
}
/**
 * What an adapter receives. Never the raw token; the principal is the resolved
 * merchant principal, not anything from the request body.
 */
export interface VerifiedContext {
    /** Merchant principal resolved from the pairwise subject by PrincipalResolver. */
    principal: string;
    /** Pairwise subject from the token (`buyer_pairwise_*` / `guest_pairwise_*`). */
    subject: string;
    actionId: string;
    jti: string;
    operation: string;
    contractVersion: string;
    expectedRevision?: number;
    installationId: string;
    /** Decoded path-template bindings, e.g. { cart_id: "cart_..." }. */
    pathParams: Record<string, string>;
}
export type Adapter = (ctx: VerifiedContext, input: unknown) => Promise<unknown> | unknown;
export type AdapterMap = Record<string, Adapter>;
/** Injectable clock, returns unix seconds (fractional ok). Defaults to Date.now(). */
export type Clock = () => number;
export declare function defaultClock(): number;
