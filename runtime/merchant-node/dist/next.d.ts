import type { MerchantRuntime } from "./runtime.js";
type NextHandler = (req: Request) => Promise<Response>;
export declare function createNextHandlers(runtime: MerchantRuntime): Record<string, NextHandler>;
export {};
