import type { FastifyPluginCallback, FastifyReply, FastifyRequest } from "fastify";
import type { DiscoveryHandler } from "./discovery.js";
import type { MerchantRuntime } from "./runtime.js";
export interface AutericFastifyOptions {
    runtime: MerchantRuntime;
}
export declare const autericFastifyPlugin: FastifyPluginCallback<AutericFastifyOptions>;
/** Fastify handler for /.well-known/ucp. Register at the app root. */
export declare function createFastifyDiscoveryHandler(discovery: DiscoveryHandler): (req: FastifyRequest, reply: FastifyReply) => Promise<void>;
export default autericFastifyPlugin;
