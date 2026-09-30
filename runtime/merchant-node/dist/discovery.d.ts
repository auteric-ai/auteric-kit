import type { Clock, ResponseLike, TrustBundle } from "./types.js";
export declare const DISCOVERY_MAX_TTL_SECONDS = 30;
export interface SignedDiscoveryDocument {
    /** The unsigned UCP document object from the control plane. */
    document: unknown;
    /** kid of the discovery signing key (must be pinned in trust.discoveryKeys). */
    kid: string;
    /** base64url Ed25519 signature over canonicalizeJcs(document). */
    signature: string;
}
/** Pluggable source for the pinned control plane (HTTPS fetch in production). */
export interface DiscoveryDocumentFetcher {
    fetch(): Promise<SignedDiscoveryDocument>;
}
export interface DiscoveryHandlerOptions {
    fetcher: DiscoveryDocumentFetcher;
    trust: TrustBundle;
    storeId: string;
    hostname: string;
    clock?: Clock;
    /** Response Cache-Control max-age cap; spec ceiling is 30s. */
    maxTtlSeconds?: number;
}
export declare class DiscoveryError extends Error {
    constructor(message: string);
}
export interface DiscoveryHandler {
    handle(headers?: Record<string, string | string[] | undefined>): Promise<ResponseLike>;
}
export declare function createDiscoveryHandler(opts: DiscoveryHandlerOptions): DiscoveryHandler;
