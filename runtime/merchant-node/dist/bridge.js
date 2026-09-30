import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { MerchantError } from "./errors.js";
const MAX_BODY_BYTES = 1024 * 1024;
function sameToken(supplied, expected) {
    const left = Buffer.from(supplied);
    const right = Buffer.from(`Bearer ${expected}`);
    return left.length === right.length && timingSafeEqual(left, right);
}
function respond(response, status, body) {
    const encoded = JSON.stringify(body);
    response.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(encoded) });
    response.end(encoded);
}
/**
 * A deliberately tiny loopback bridge for custom Node services. Authentication,
 * schema validation and idempotency stay in the sidecar; this bridge only calls
 * merchant-owned business functions with the already verified identity context.
 */
export function createServiceBridge(options) {
    if (!options.token || options.token.length < 32)
        throw new Error("bridge token must contain at least 32 characters");
    const host = options.host ?? "127.0.0.1";
    if (!["127.0.0.1", "::1", "localhost"].includes(host))
        throw new Error("service bridge may listen only on loopback");
    const server = createServer((request, response) => {
        void (async () => {
            if (!sameToken(String(request.headers.authorization ?? ""), options.token)) {
                respond(response, 401, { error: "unauthorized" });
                return;
            }
            if (request.method === "GET" && request.url === "/health/ready") {
                respond(response, 200, { status: "ready", operations: Object.keys(options.adapters).sort() });
                return;
            }
            const match = request.method === "POST" ? /^\/invoke\/([a-z][a-z0-9_]*)$/.exec(request.url ?? "") : null;
            if (!match || !options.adapters[match[1]]) {
                respond(response, 404, { error: "operation_not_found" });
                return;
            }
            const chunks = [];
            let size = 0;
            for await (const chunk of request) {
                const bytes = Buffer.from(chunk);
                size += bytes.length;
                if (size > MAX_BODY_BYTES) {
                    respond(response, 413, { error: "request_too_large" });
                    request.destroy();
                    return;
                }
                chunks.push(bytes);
            }
            let envelope;
            try {
                envelope = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            }
            catch {
                respond(response, 400, { error: "invalid_json" });
                return;
            }
            const context = envelope.context;
            if (!context || context.operation !== match[1] || !context.principal || !context.actionId || !context.installationId) {
                respond(response, 400, { error: "invalid_context" });
                return;
            }
            try {
                const result = await options.adapters[match[1]](Object.freeze({ ...context }), envelope.input);
                respond(response, 200, result);
            }
            catch (error) {
                if (error instanceof MerchantError) {
                    respond(response, error.httpStatus, { error: {
                            code: error.code, message: "merchant rejected the action", retryable: error.retryable,
                            action_id: context.actionId,
                        } });
                    return;
                }
                respond(response, 502, { error: "merchant_service_failed" });
            }
        })().catch(() => respond(response, 500, { error: "bridge_failure" }));
    });
    return {
        server,
        listen: () => new Promise((resolve, reject) => {
            server.once("error", reject);
            server.listen(options.port ?? 0, host, () => {
                server.off("error", reject);
                const address = server.address();
                if (!address || typeof address === "string")
                    return reject(new Error("unexpected bridge address"));
                resolve({ host, port: address.port });
            });
        }),
        close: () => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
    };
}
