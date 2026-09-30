// MEP/1 §2 execution-JWT verification. Ed25519 only, via node:crypto — no
// runtime dependencies. Implements the §2.2 verification order (steps 3-10);
// bounded parsing (step 1) and raw path/query capture (step 2) happen in the
// runtime/driver before this is called, and principal resolution (step 11)
// happens after.
import { createPublicKey, verify as cryptoVerify } from "node:crypto";
import { MerchantError } from "./errors.js";
import { defaultClock } from "./types.js";
export const CLOCK_SKEW_SECONDS = 5;
export const MAX_TOKEN_WINDOW_SECONDS = 30;
export const NONCE_TTL_SECONDS = 60;
export const MAX_TOKEN_BYTES = 8 * 1024;
/**
 * TESTS/DEV ONLY. Process-local nonce cache; offers no protection across
 * instances or restarts. Lazy expiry on access — deliberately uses no timers
 * so the core stays serverless-safe.
 */
export class InMemoryNonceCache {
    seen = new Map();
    clock;
    constructor(clock = defaultClock) {
        this.clock = clock;
    }
    async claim(jti, ttlSeconds) {
        const now = this.clock();
        for (const [key, expiresAt] of this.seen) {
            if (expiresAt <= now)
                this.seen.delete(key);
        }
        if (this.seen.has(jti))
            return false;
        this.seen.set(jti, now + ttlSeconds);
        return true;
    }
}
function unauthenticated(message) {
    return new MerchantError("UNAUTHENTICATED", message);
}
function forbidden(message) {
    return new MerchantError("FORBIDDEN", message);
}
function b64urlDecode(part) {
    // Strict base64url: no whitespace, no standard-alphabet chars.
    if (!/^[A-Za-z0-9_-]*$/.test(part))
        throw unauthenticated("malformed token encoding");
    return Buffer.from(part, "base64url");
}
function parseJsonPart(part, what) {
    let buf;
    try {
        buf = b64urlDecode(part);
    }
    catch (err) {
        if (err instanceof MerchantError)
            throw err;
        throw unauthenticated(`malformed token ${what}`);
    }
    if (buf.length > MAX_TOKEN_BYTES)
        throw unauthenticated(`token ${what} too large`);
    try {
        const parsed = JSON.parse(buf.toString("utf8"));
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("not an object");
        }
        return parsed;
    }
    catch {
        throw unauthenticated(`malformed token ${what}`);
    }
}
export function importEd25519PublicKey(encoded) {
    if (encoded.startsWith("pem:")) {
        return createPublicKey(encoded.slice(4));
    }
    if (encoded.startsWith("spki:")) {
        return createPublicKey({ key: Buffer.from(encoded.slice(5), "base64"), format: "der", type: "spki" });
    }
    let raw = null;
    if (/^[0-9a-fA-F]{64}$/.test(encoded)) {
        raw = Buffer.from(encoded, "hex");
    }
    else if (/^[A-Za-z0-9_-]{43}$/.test(encoded) || /^[A-Za-z0-9+/]{43}=?$/.test(encoded)) {
        raw = Buffer.from(encoded, "base64url");
    }
    if (raw && raw.length === 32) {
        return createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: raw.toString("base64url") }, format: "jwk" });
    }
    throw new Error("unsupported Ed25519 key encoding (want raw 32-byte base64url/hex, pem:, or spki:)");
}
function requireString(claims, name) {
    const value = claims[name];
    if (typeof value !== "string" || value.length === 0) {
        throw unauthenticated(`missing or invalid claim: ${name}`);
    }
    return value;
}
function requireNumericDate(claims, name) {
    const value = claims[name];
    if (typeof value !== "number" || !Number.isFinite(value)) {
        throw unauthenticated(`missing or invalid claim: ${name}`);
    }
    return value;
}
/**
 * Verify a signed execution JWT end to end. Throws MerchantError with the
 * §5 wire code on any failure; returns the validated claims on success.
 */
export async function verifyExecutionToken(token, opts) {
    const clock = opts.clock ?? defaultClock;
    // --- structure ---
    if (Buffer.byteLength(token, "utf8") > MAX_TOKEN_BYTES) {
        throw unauthenticated("token too large");
    }
    const parts = token.split(".");
    if (parts.length !== 3)
        throw unauthenticated("malformed token");
    const [headerPart, payloadPart, signaturePart] = parts;
    const header = parseJsonPart(headerPart, "header");
    const claimsRaw = parseJsonPart(payloadPart, "payload");
    let signature;
    try {
        signature = b64urlDecode(signaturePart);
    }
    catch (err) {
        if (err instanceof MerchantError)
            throw err;
        throw unauthenticated("malformed token signature");
    }
    // --- header policy: EdDSA only, pinned keys only (MEP/1 §2) ---
    if (header.alg !== "EdDSA")
        throw unauthenticated("alg must be EdDSA");
    if (header.typ !== "auteric-exec+jwt")
        throw unauthenticated("unexpected token typ");
    if ("jku" in header || "x5u" in header) {
        throw unauthenticated("jku/x5u headers are never accepted");
    }
    if (typeof header.kid !== "string" || header.kid.length === 0) {
        throw unauthenticated("missing kid");
    }
    const kid = header.kid;
    // --- §2.2 step 3: issuer allowlist -> pinned kid -> Ed25519 signature ---
    const iss = requireString(claimsRaw, "iss");
    if (!opts.trust.issuers.includes(iss))
        throw unauthenticated("issuer not allowed");
    const keyEncoded = opts.trust.keys[kid];
    if (!keyEncoded)
        throw unauthenticated("unknown kid");
    let key;
    try {
        key = importEd25519PublicKey(keyEncoded);
    }
    catch {
        throw unauthenticated("unusable pinned key");
    }
    const signed = Buffer.from(`${headerPart}.${payloadPart}`, "utf8");
    if (!cryptoVerify(null, signed, key, signature)) {
        throw unauthenticated("invalid signature");
    }
    // --- §2.2 step 4: temporal window, skew <= 5s ---
    const iat = requireNumericDate(claimsRaw, "iat");
    const exp = requireNumericDate(claimsRaw, "exp");
    const now = clock();
    if (now > exp + CLOCK_SKEW_SECONDS)
        throw unauthenticated("token expired");
    if (iat > now + CLOCK_SKEW_SECONDS)
        throw unauthenticated("token issued in the future");
    if (exp - iat > MAX_TOKEN_WINDOW_SECONDS)
        throw unauthenticated("token window exceeds 30s");
    // --- §2.2 step 5: audience / store / environment binding ---
    const aud = requireString(claimsRaw, "aud");
    if (aud !== `urn:auteric:installation:${opts.installation.installationId}`) {
        throw forbidden("audience mismatch");
    }
    const storeId = requireString(claimsRaw, "store_id");
    if (storeId !== opts.installation.storeId)
        throw forbidden("store mismatch");
    const environment = requireString(claimsRaw, "environment");
    if (environment !== opts.installation.environment)
        throw forbidden("environment mismatch");
    // --- §2.2 step 6: operation exists in the manifest and matches the route ---
    const operation = requireString(claimsRaw, "operation");
    if (!opts.installation.operations.includes(operation)) {
        throw forbidden("operation not in installation manifest");
    }
    if (operation !== opts.route.operation) {
        throw forbidden("token operation does not match request route");
    }
    // --- §2.2 step 7: request hash (§3) ---
    const requestHashClaim = requireString(claimsRaw, "request_hash");
    if (requestHashClaim !== `sha256:${opts.computedRequestHash}`) {
        throw unauthenticated("request hash mismatch");
    }
    // --- §2.2 step 8: binding digest ---
    const bindingDigest = requireString(claimsRaw, "binding_digest");
    if (bindingDigest !== opts.installation.bindingDigest) {
        throw new MerchantError("CONTRACT_MISMATCH", "binding digest mismatch");
    }
    // --- §2.2 step 9: jti nonce (replay rejection; cache >= 60s) ---
    const jti = requireString(claimsRaw, "jti");
    if (!jti.startsWith("attempt_"))
        throw unauthenticated("malformed jti");
    const ttl = Math.max(NONCE_TTL_SECONDS, Math.ceil(exp + CLOCK_SKEW_SECONDS - now));
    if (!(await opts.nonceCache.claim(jti, ttl))) {
        throw unauthenticated("replayed jti");
    }
    // --- §2.2 step 10: installation enabled ---
    if (!opts.installation.enabled) {
        throw new MerchantError("CAPABILITY_DISABLED", "installation is disabled");
    }
    const sub = requireString(claimsRaw, "sub");
    const isGatewayProbe = sub === "gateway_health_probe" || sub === "gateway_reconcile_probe";
    if ((!sub.startsWith("buyer_") && !sub.startsWith("guest_"))
        && !(opts.allowGatewayProbeSubject && isGatewayProbe)) {
        throw unauthenticated("malformed sub");
    }
    const actionId = requireString(claimsRaw, "action_id");
    if (!actionId.startsWith("action_"))
        throw unauthenticated("malformed action_id");
    return {
        iss,
        aud,
        sub,
        store_id: storeId,
        environment,
        operation,
        contract_version: requireString(claimsRaw, "contract_version"),
        binding_digest: bindingDigest,
        action_id: actionId,
        jti,
        request_hash: requestHashClaim,
        iat,
        exp,
    };
}
