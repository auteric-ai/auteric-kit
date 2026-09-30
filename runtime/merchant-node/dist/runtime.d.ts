import { type NonceCache } from "./auth.js";
import { type ExecutionStore } from "./executionStore.js";
import type { PrincipalResolver } from "./principalResolver.js";
import type { AdapterMap, Clock, InstallationManifest, RequestLike, ResponseLike, TrustBundle } from "./types.js";
export interface MerchantRuntimeOptions {
    installation: InstallationManifest;
    trust: TrustBundle;
    adapters: AdapterMap;
    /** Durable ledger in production; defaults to the test-only in-memory store. */
    executionStore?: ExecutionStore;
    principalResolver: PrincipalResolver;
    /** Shared durable nonce cache in production; defaults to test-only in-memory. */
    nonceCache?: NonceCache;
    clock?: Clock;
}
export interface MerchantRuntime {
    handle(request: RequestLike): Promise<ResponseLike>;
    /** Operations actually routed (manifest ∩ registry ∩ adapters). */
    readonly operations: readonly string[];
}
export declare function createMerchantRuntime(opts: MerchantRuntimeOptions): MerchantRuntime;
