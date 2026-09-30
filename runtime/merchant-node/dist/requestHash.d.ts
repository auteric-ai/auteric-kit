export declare class RequestHashError extends Error {
    constructor(message: string);
}
/** Decode every %XX triplet exactly once; literal text passes through as UTF-8. */
export declare function pctDecodeOnce(raw: string): string;
/** NFC-normalize and re-encode: unreserved + extraLiterals stay literal. */
export declare function pctEncode(decoded: string, extraLiterals: string): string;
export declare function canonicalPath(rawPath: string, trustedProxyPrefix?: string | null): string;
export declare function canonicalQuery(rawQuery: string): string;
export declare function sha256Hex(data: Buffer | string): string;
/**
 * request_hash = SHA-256 hex of
 * "<METHOD>\n<canonical_path>\n<canonical_query>\nSHA256_HEX(<raw_body_bytes>)".
 */
export declare function requestHash(method: string, rawPath: string, rawQuery: string, body: Buffer, trustedProxyPrefix?: string | null): string;
