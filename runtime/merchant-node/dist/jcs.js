// Minimal RFC 8785 (JCS) canonicalization for the JSON subset used by
// discovery documents: objects, arrays, strings, numbers, booleans, null.
// Keys sorted by UTF-16 code units (per RFC 8785), no whitespace, numbers via
// ECMAScript JSON.stringify semantics. Non-finite numbers and undefined/
// function/symbol/bigint values are rejected.
export class JcsError extends Error {
    constructor(message) {
        super(message);
        this.name = "JcsError";
    }
}
export function canonicalizeJcs(value) {
    if (value === null)
        return "null";
    switch (typeof value) {
        case "boolean":
            return value ? "true" : "false";
        case "number": {
            if (!Number.isFinite(value))
                throw new JcsError("non-finite number is not canonicalizable");
            return JSON.stringify(value);
        }
        case "string":
            return JSON.stringify(value);
        case "object": {
            if (Array.isArray(value)) {
                return `[${value.map((item) => canonicalizeJcs(item)).join(",")}]`;
            }
            const entries = Object.entries(value);
            entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
            const parts = entries.map(([k, v]) => {
                if (v === undefined || typeof v === "function" || typeof v === "symbol" || typeof v === "bigint") {
                    throw new JcsError("non-JSON value is not canonicalizable");
                }
                return `${JSON.stringify(k)}:${canonicalizeJcs(v)}`;
            });
            return `{${parts.join(",")}}`;
        }
        default:
            throw new JcsError("non-JSON value is not canonicalizable");
    }
}
