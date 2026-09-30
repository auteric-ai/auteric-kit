// MEP/1 §6 + plan §15: serves /.well-known/ucp. The signed document comes from
// the pinned Auteric control plane through a pluggable fetcher; the store
// verifies the Ed25519 signature over the JCS (RFC 8785) canonical form and
// never signs or fabricates a document locally.
import { verify as cryptoVerify } from "node:crypto";
import { importEd25519PublicKey } from "./auth.js";
import { canonicalizeJcs, JcsError } from "./jcs.js";
import { sha256Hex } from "./requestHash.js";
import { defaultClock } from "./types.js";
export const DISCOVERY_MAX_TTL_SECONDS = 30;
const DISCOVERY_SKEW_SECONDS = 5;
export class DiscoveryError extends Error {
    constructor(message) {
        super(message);
        this.name = "DiscoveryError";
    }
}
function asStringField(doc, field) {
    const value = doc[field];
    if (typeof value !== "string" || value.length === 0) {
        throw new DiscoveryError(`discovery document missing ${field}`);
    }
    return value;
}
function asNumericField(doc, field) {
    const value = doc[field];
    if (typeof value !== "number" || !Number.isFinite(value)) {
        throw new DiscoveryError(`discovery document missing ${field}`);
    }
    return value;
}
export function createDiscoveryHandler(opts) {
    const clock = opts.clock ?? defaultClock;
    const ttl = Math.min(opts.maxTtlSeconds ?? DISCOVERY_MAX_TTL_SECONDS, DISCOVERY_MAX_TTL_SECONDS);
    const discoveryKeys = opts.trust.discoveryKeys ?? {};
    let cached = null;
    async function fetchAndVerify() {
        const now = clock();
        const signed = await opts.fetcher.fetch();
        const keyEncoded = discoveryKeys[signed.kid];
        if (!keyEncoded)
            throw new DiscoveryError("unknown discovery kid");
        let key;
        try {
            key = importEd25519PublicKey(keyEncoded);
        }
        catch {
            throw new DiscoveryError("unusable pinned discovery key");
        }
        let canonical;
        try {
            canonical = canonicalizeJcs(signed.document);
        }
        catch (err) {
            throw new DiscoveryError(err instanceof JcsError ? err.message : "document not canonicalizable");
        }
        let signature;
        try {
            signature = Buffer.from(signed.signature, "base64url");
        }
        catch {
            throw new DiscoveryError("malformed discovery signature");
        }
        if (!cryptoVerify(null, Buffer.from(canonical, "utf8"), key, signature)) {
            throw new DiscoveryError("invalid discovery signature");
        }
        const doc = signed.document;
        if (asStringField(doc, "store_id") !== opts.storeId) {
            throw new DiscoveryError("store_id mismatch");
        }
        if (asStringField(doc, "hostname") !== opts.hostname) {
            throw new DiscoveryError("hostname mismatch");
        }
        const issuedAt = asNumericField(doc, "issued_at");
        const expiresAt = asNumericField(doc, "expires_at");
        if (issuedAt > now + DISCOVERY_SKEW_SECONDS) {
            throw new DiscoveryError("document issued in the future");
        }
        if (expiresAt <= now) {
            throw new DiscoveryError("document expired");
        }
        return { canonical, etag: `"${sha256Hex(canonical)}"`, expiresAt, fetchedAt: now };
    }
    function serve(doc, ifNoneMatch) {
        const now = clock();
        const maxAge = Math.max(0, Math.min(ttl, Math.floor(doc.expiresAt - now)));
        const headers = {
            "content-type": "application/json",
            "x-content-type-options": "nosniff",
            etag: doc.etag,
            "cache-control": `public, max-age=${maxAge}`,
        };
        if (ifNoneMatch && ifNoneMatch === doc.etag) {
            return { status: 304, headers, body: "" };
        }
        return { status: 200, headers, body: doc.canonical };
    }
    return {
        async handle(headers = {}) {
            const now = clock();
            const inm = headers["if-none-match"];
            const ifNoneMatch = Array.isArray(inm) ? inm[0] : inm;
            const freshEnough = cached && now - cached.fetchedAt < ttl && now < cached.expiresAt;
            if (freshEnough && cached)
                return serve(cached, ifNoneMatch);
            try {
                cached = await fetchAndVerify();
                return serve(cached, ifNoneMatch);
            }
            catch {
                // Failure policy (plan §15.2): last-known-valid is served only within
                // its own expiry; beyond that the failure is explicit. Nothing is ever
                // signed locally.
                if (cached && now < cached.expiresAt)
                    return serve(cached, ifNoneMatch);
                return {
                    status: 503,
                    headers: {
                        "content-type": "application/json",
                        "x-content-type-options": "nosniff",
                        "cache-control": "no-store",
                    },
                    body: JSON.stringify({ error: "ucp document unavailable" }),
                };
            }
        },
    };
}
