// Shared types for the merchant runtime. Framework drivers (express/fastify/next)
// adapt their native request objects into `RequestLike`; the runtime only ever
// sees this shape.
export function defaultClock() {
    return Date.now() / 1000;
}
