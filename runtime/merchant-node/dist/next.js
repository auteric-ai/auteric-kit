async function dispatch(runtime, req) {
    const url = new URL(req.url);
    const body = Buffer.from(await req.arrayBuffer());
    const headers = {};
    req.headers.forEach((value, name) => {
        headers[name.toLowerCase()] = value;
    });
    const out = await runtime.handle({
        method: req.method,
        rawPath: url.pathname,
        rawQuery: url.search.startsWith("?") ? url.search.slice(1) : url.search,
        headers,
        body,
    });
    return new Response(out.body, { status: out.status, headers: out.headers });
}
export function createNextHandlers(runtime) {
    const make = () => (req) => dispatch(runtime, req);
    return {
        GET: make(),
        HEAD: make(),
        POST: make(),
        PUT: make(),
        PATCH: make(),
        DELETE: make(),
        OPTIONS: make(),
    };
}
