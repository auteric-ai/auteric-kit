// Framework-free merchant runtime for MEP/1. The runtime owns the full §2.2
// verification order; merchant adapters only ever see a VerifiedContext plus a
// contract-validated input — never a raw token, and never a principal taken
// from the request body.
//
// Serverless note: the core performs no filesystem writes and starts no timers.
// The default in-memory nonce cache / execution store expire lazily on access.
// SQLiteExecutionStore (explicit opt-in) is the only component that touches the
// filesystem.
import { InMemoryNonceCache, verifyExecutionToken, } from "./auth.js";
import { MerchantError, toWireError } from "./errors.js";
import { InMemoryExecutionStore, } from "./executionStore.js";
import { canonicalPath, canonicalQuery, pctDecodeOnce, requestHash, RequestHashError } from "./requestHash.js";
import { defaultClock } from "./types.js";
import { getOperationContract, validateOperationInput, validateOperationOutput } from "./validation.js";
function compilePathTemplate(template) {
    return template
        .split("/")
        .slice(1)
        .map((seg) => {
        const m = /^\{([a-z_][a-z0-9_]*)\}$/.exec(seg);
        return m ? { kind: "param", name: m[1] } : { kind: "literal", value: seg };
    });
}
function matchRoute(routes, method, canonPath) {
    const segments = canonPath.split("/").slice(1);
    for (const route of routes) {
        if (route.method !== method)
            continue;
        if (route.segments.length !== segments.length)
            continue;
        const pathParams = {};
        let ok = true;
        for (let i = 0; i < segments.length; i++) {
            const seg = route.segments[i];
            if (seg.kind === "literal") {
                if (seg.value !== segments[i]) {
                    ok = false;
                    break;
                }
            }
            else {
                // Canonical path segments are NFC + pct-encoded; decode once for the adapter.
                pathParams[seg.name] = pctDecodeOnce(segments[i]);
            }
        }
        if (ok)
            return { route, pathParams };
    }
    return null;
}
const JSON_HEADERS = {
    "content-type": "application/json",
    "x-content-type-options": "nosniff",
};
function jsonResponse(status, body) {
    return { status, headers: { ...JSON_HEADERS }, body: JSON.stringify(body) };
}
function errorResponse(status, body) {
    return { status, headers: { ...JSON_HEADERS }, body: JSON.stringify(body) };
}
/** Query-string -> input object for bodiless methods. Integer/boolean-looking
 * values are coerced (schemas demand real ints); repeated keys: last wins. */
function inputFromQuery(rawQuery) {
    const out = {};
    if (!rawQuery)
        return out;
    for (const pair of rawQuery.split("&")) {
        const eq = pair.indexOf("=");
        const rawKey = eq === -1 ? pair : pair.slice(0, eq);
        const rawValue = eq === -1 ? "" : pair.slice(eq + 1);
        const key = pctDecodeOnce(rawKey.replace(/\+/g, " "));
        const value = pctDecodeOnce(rawValue.replace(/\+/g, " "));
        out[key] = coerceQueryValue(value);
    }
    return out;
}
function coerceQueryValue(value) {
    if (/^-?\d+$/.test(value)) {
        const n = Number(value);
        if (Number.isSafeInteger(n))
            return n;
    }
    if (value === "true")
        return true;
    if (value === "false")
        return false;
    return value;
}
const BODILESS_METHODS = new Set(["GET", "HEAD", "DELETE"]);
export function createMerchantRuntime(opts) {
    const clock = opts.clock ?? defaultClock;
    const executionStore = opts.executionStore ?? new InMemoryExecutionStore(clock);
    const nonceCache = opts.nonceCache ?? new InMemoryNonceCache(clock);
    const maxBodyBytes = opts.installation.maxBodyBytes ?? 1024 * 1024;
    // Fail fast at composition time: every manifest operation must be a known
    // registry operation with a registered adapter. Adapters outside the
    // manifest are simply never routed.
    const routes = [];
    for (const operation of opts.installation.operations) {
        const contract = getOperationContract(operation);
        if (!contract) {
            throw new Error(`manifest operation not in registry: ${operation}`);
        }
        if (!opts.adapters[operation]) {
            throw new Error(`manifest operation has no adapter: ${operation}`);
        }
        routes.push({
            operation,
            method: contract.method,
            pathTemplate: contract.path,
            segments: compilePathTemplate(contract.path),
            idempotencyRequired: contract.idempotency === "required",
        });
    }
    // A literal route must win over a parameter route of the same method and
    // segment count: `/products/search` is not a product whose id is `search`.
    // Installation operation order is data, not a routing precedence contract.
    routes.sort((a, b) => {
        const literals = (route) => route.segments.filter((segment) => segment.kind === "literal").length;
        const specificity = literals(b) - literals(a);
        return specificity || a.pathTemplate.localeCompare(b.pathTemplate);
    });
    async function execute(request) {
        // §2.2 step 1: bounded parsing.
        if (request.body.length > maxBodyBytes) {
            throw new MerchantError("INVALID_INPUT", "request body too large");
        }
        // §2.2 step 2: raw path/query arrive pre-normalization from the driver.
        let canonPath;
        let hash;
        try {
            canonPath = canonicalPath(request.rawPath, opts.installation.trustedProxyPrefix ?? null);
            canonicalQuery(request.rawQuery); // validate canonicalizability early
            hash = requestHash(request.method, request.rawPath, request.rawQuery, request.body, opts.installation.trustedProxyPrefix ?? null);
        }
        catch (err) {
            if (err instanceof RequestHashError) {
                throw new MerchantError("UNAUTHENTICATED", "request target is not canonicalizable");
            }
            throw err;
        }
        const method = request.method.toUpperCase();
        const match = matchRoute(routes, method, canonPath);
        if (!match) {
            // Only manifest operations are served; anything else is denied.
            throw new MerchantError("RESOURCE_NOT_FOUND", "unknown operation route");
        }
        const authHeader = request.headers["authorization"];
        const bearer = Array.isArray(authHeader) ? authHeader[0] : authHeader;
        if (!bearer || !bearer.startsWith("Bearer ")) {
            throw new MerchantError("UNAUTHENTICATED", "missing bearer token");
        }
        // §2.2 steps 3-10.
        const claims = await verifyExecutionToken(bearer.slice(7), {
            trust: opts.trust,
            installation: opts.installation,
            route: { operation: match.route.operation, method: match.route.method },
            computedRequestHash: hash,
            nonceCache,
            clock,
        });
        // §2.2 step 11: principal resolution. `sub` is pairwise; the merchant
        // resolver maps it. A principal field inside the body is never consulted.
        const principal = await opts.principalResolver.resolve(claims.sub, {
            installationId: opts.installation.installationId,
            operation: claims.operation,
        });
        if (!principal)
            throw new MerchantError("FORBIDDEN", "unknown principal");
        const ctx = {
            principal,
            subject: claims.sub,
            actionId: claims.action_id,
            jti: claims.jti,
            operation: claims.operation,
            contractVersion: claims.contract_version,
            installationId: opts.installation.installationId,
            pathParams: match.pathParams,
        };
        // §2.2 step 12: idempotency on action_id + request_hash.
        const key = {
            installationId: opts.installation.installationId,
            principal,
            operation: claims.operation,
            actionId: claims.action_id,
        };
        const useLedger = match.route.idempotencyRequired;
        if (useLedger) {
            const decision = await executionStore.reserve(key, hash);
            if (decision.kind === "replay") {
                return {
                    status: decision.outcome.status,
                    headers: { ...JSON_HEADERS },
                    body: decision.outcome.body,
                };
            }
            if (decision.kind === "conflict") {
                throw new MerchantError("IDEMPOTENCY_CONFLICT", "same action_id with a different payload");
            }
            if (decision.kind === "in_flight" || decision.kind === "uncertain") {
                throw new MerchantError("EXECUTION_UNCERTAIN", "a previous attempt is still being reconciled", { action_id: claims.action_id });
            }
            await executionStore.markExecuting(key);
        }
        // §2.2 step 13: input validation -> handler -> output validation.
        let input;
        try {
            if (BODILESS_METHODS.has(method)) {
                input = inputFromQuery(request.rawQuery);
            }
            else if (request.body.length === 0) {
                input = {};
            }
            else {
                try {
                    input = JSON.parse(request.body.toString("utf8"));
                }
                catch {
                    throw new MerchantError("INVALID_INPUT", "request body is not valid JSON");
                }
            }
            validateOperationInput(claims.operation, input);
        }
        catch (err) {
            const wire = toWireError(err, claims.action_id);
            if (useLedger) {
                await executionStore.fail(key, { status: wire.status, body: JSON.stringify(wire.body), isError: true });
            }
            return errorResponse(wire.status, wire.body);
        }
        const inputRecord = input;
        if (typeof inputRecord.expected_revision === "number") {
            ctx.expectedRevision = inputRecord.expected_revision;
        }
        let output;
        try {
            output = await opts.adapters[claims.operation](ctx, input);
        }
        catch (err) {
            if (useLedger) {
                if (err instanceof MerchantError) {
                    // Definitive typed failure: replay of this action_id returns the same error.
                    const wire = toWireError(err, claims.action_id);
                    await executionStore.fail(key, { status: wire.status, body: JSON.stringify(wire.body), isError: true });
                    return errorResponse(wire.status, wire.body);
                }
                // Unknown failure mid-handler: the write may have happened.
                await executionStore.markUncertain(key);
                throw new MerchantError("EXECUTION_UNCERTAIN", "execution outcome is being reconciled", {
                    action_id: claims.action_id,
                });
            }
            throw err;
        }
        try {
            validateOperationOutput(claims.operation, output);
        }
        catch (err) {
            // Adapter broke the contract; the write may have happened. Never pass
            // the invalid payload through.
            if (useLedger)
                await executionStore.markUncertain(key);
            throw err;
        }
        const body = JSON.stringify(output);
        if (useLedger) {
            await executionStore.complete(key, { status: 200, body, isError: false });
        }
        return { status: 200, headers: { ...JSON_HEADERS }, body };
    }
    return {
        operations: routes.map((r) => r.operation),
        async handle(request) {
            try {
                return await execute(request);
            }
            catch (err) {
                const actionId = err instanceof MerchantError ? err.details?.action_id : undefined;
                const wire = toWireError(err, actionId);
                return errorResponse(wire.status, wire.body);
            }
        },
    };
}
