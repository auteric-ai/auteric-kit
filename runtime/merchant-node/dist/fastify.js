function send(reply, out) {
    void reply.code(out.status);
    for (const [name, value] of Object.entries(out.headers))
        void reply.header(name, value);
    void reply.send(out.body);
}
async function handleRequest(runtime, req, reply) {
    const rawTarget = req.raw.url ?? "/";
    const q = rawTarget.indexOf("?");
    const rawPath = q === -1 ? rawTarget : rawTarget.slice(0, q);
    const rawQuery = q === -1 ? "" : rawTarget.slice(q + 1);
    const body = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
    const out = await runtime.handle({
        method: req.method,
        rawPath,
        rawQuery,
        headers: req.headers,
        body,
    });
    send(reply, out);
}
export const autericFastifyPlugin = (fastify, opts, done) => {
    const parseAsBuffer = (_req, body, done) => done(null, body);
    // Raw bytes for hashing: override the default JSON parser and add a
    // catch-all, scoped to this plugin's encapsulated context.
    fastify.addContentTypeParser("application/json", { parseAs: "buffer" }, parseAsBuffer);
    fastify.addContentTypeParser("*", { parseAs: "buffer" }, parseAsBuffer);
    fastify.route({
        method: ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        url: "/*",
        handler: (req, reply) => handleRequest(opts.runtime, req, reply),
    });
    done();
};
/** Fastify handler for /.well-known/ucp. Register at the app root. */
export function createFastifyDiscoveryHandler(discovery) {
    return async (req, reply) => {
        const out = await discovery.handle(req.headers);
        send(reply, out);
    };
}
export default autericFastifyPlugin;
