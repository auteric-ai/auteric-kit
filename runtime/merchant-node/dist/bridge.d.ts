import { type Server } from "node:http";
import type { AdapterMap } from "./types.js";
export interface ServiceBridgeOptions {
    adapters: AdapterMap;
    token: string;
    host?: string;
    port?: number;
}
export interface ServiceBridge {
    server: Server;
    listen(): Promise<{
        host: string;
        port: number;
    }>;
    close(): Promise<void>;
}
/**
 * A deliberately tiny loopback bridge for custom Node services. Authentication,
 * schema validation and idempotency stay in the sidecar; this bridge only calls
 * merchant-owned business functions with the already verified identity context.
 */
export declare function createServiceBridge(options: ServiceBridgeOptions): ServiceBridge;
