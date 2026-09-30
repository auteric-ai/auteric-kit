import { Router, type RequestHandler } from "express";
import type { DiscoveryHandler } from "./discovery.js";
import type { MerchantRuntime } from "./runtime.js";
export interface AutericRouterOptions {
    /**
     * Reserved proxy trust flag. The SDK never reads X-Forwarded-* for
     * authentication; when false (default) those headers are ignored entirely.
     * Proxy mount prefixes belong in installation.trustedProxyPrefix.
     */
    trustProxy?: boolean;
    /** express.raw limit; must exceed the largest body the runtime accepts. */
    rawBodyLimit?: string | number;
}
export declare function createAutericRouter(runtime: MerchantRuntime, opts?: AutericRouterOptions): Router;
/** Express handler for /.well-known/ucp. Register before the SPA catch-all. */
export declare function createExpressDiscoveryHandler(discovery: DiscoveryHandler): RequestHandler;
