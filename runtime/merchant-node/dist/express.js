// Express 4 driver. Mount BEFORE any express.json() / SPA catch-all:
//
//   app.use("/api/auteric/v1", createAutericRouter(runtime));
//   app.get("/.well-known/ucp", createExpressDiscoveryHandler(discovery));
//   app.use(express.json());            // after the auteric mount
//   app.get("*", spaHandler);           // SPA catch-all last
//
// The router applies express.raw itself so the exact body bytes reach the
// MEP/1 §3 request hash. If a global express.json() ran first, the raw bytes
// are gone and every request will fail authentication — mount order matters.
import { Router, raw } from "express";
function send(res, out) {
    res.status(out.status);
    for (const [name, value] of Object.entries(out.headers))
        res.setHeader(name, value);
    res.send(out.body);
}
export function createAutericRouter(runtime, opts = {}) {
    const router = Router();
    const limit = opts.rawBodyLimit ?? "2mb";
    router.use(raw({ type: () => true, limit }));
    router.all("*", (req, res) => {
        void (async () => {
            const q = req.originalUrl.indexOf("?");
            const rawPath = q === -1 ? req.originalUrl : req.originalUrl.slice(0, q);
            const rawQuery = q === -1 ? "" : req.originalUrl.slice(q + 1);
            const body = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
            const out = await runtime.handle({
                method: req.method,
                rawPath,
                rawQuery,
                headers: req.headers,
                body,
            });
            send(res, out);
        })().catch(() => {
            // runtime.handle normalizes all MEP errors; this is the last-resort net.
            res.status(502).setHeader("content-type", "application/json");
            res.setHeader("x-content-type-options", "nosniff");
            res.send(JSON.stringify({ error: { code: "UPSTREAM_ERROR", message: "Upstream error", retryable: true } }));
        });
    });
    // body-parser rejections (e.g. entity too large) -> MEP envelope.
    router.use((err, _req, res, next) => {
        const status = err?.status;
        if (status === 413) {
            res.status(400).setHeader("content-type", "application/json");
            res.setHeader("x-content-type-options", "nosniff");
            res.send(JSON.stringify({
                error: { code: "INVALID_INPUT", message: "request body too large", retryable: false },
            }));
            return;
        }
        next(err);
    });
    return router;
}
/** Express handler for /.well-known/ucp. Register before the SPA catch-all. */
export function createExpressDiscoveryHandler(discovery) {
    return (req, res) => {
        discovery
            .handle(req.headers)
            .then((out) => send(res, out))
            .catch(() => {
            res.status(503).setHeader("content-type", "application/json");
            res.send(JSON.stringify({ error: "ucp document unavailable" }));
        });
    };
}
