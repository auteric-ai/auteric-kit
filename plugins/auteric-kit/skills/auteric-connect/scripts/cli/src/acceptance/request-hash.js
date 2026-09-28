// MEP/1 §3 request-hash canonicalization (signing side). Byte-exact port of
// the locked reference (packages/commerce-contracts/tools/gen_request_hash_vectors.py,
// mirrored by packages/merchant-node/src/requestHash.ts) so the acceptance
// runner can mint execution tokens the merchant runtime will accept. Covered
// by every golden vector in packages/commerce-contracts/vectors/request-hash.json.
import { createHash } from 'node:crypto';

const UNRESERVED = new Set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~');
const HEX = '0123456789abcdefABCDEF';

export class RequestHashError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RequestHashError';
  }
}

// Decode every %XX triplet exactly once; literal text passes through as UTF-8.
export function pctDecodeOnce(raw) {
  const data = Buffer.from(raw, 'utf8');
  const out = [];
  let i = 0;
  while (i < data.length) {
    if (data[i] === 0x25) {
      if (i + 2 >= data.length) throw new RequestHashError('truncated percent-encoding');
      const hi = String.fromCharCode(data[i + 1]);
      const lo = String.fromCharCode(data[i + 2]);
      if (!HEX.includes(hi) || !HEX.includes(lo)) throw new RequestHashError(`invalid percent-encoding %${hi}${lo}`);
      out.push(parseInt(hi + lo, 16));
      i += 3;
    } else {
      out.push(data[i]);
      i += 1;
    }
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(out));
  } catch {
    throw new RequestHashError('path/query is not valid UTF-8');
  }
}

// NFC-normalize and re-encode: unreserved + extraLiterals stay literal.
export function pctEncode(decoded, extraLiterals) {
  const normalized = decoded.normalize('NFC');
  const out = [];
  for (const char of normalized) {
    if (UNRESERVED.has(char) || extraLiterals.includes(char)) {
      out.push(char);
    } else {
      for (const byte of Buffer.from(char, 'utf8')) {
        out.push(`%${byte.toString(16).toUpperCase().padStart(2, '0')}`);
      }
    }
  }
  return out.join('');
}

export function canonicalPath(rawPath, trustedProxyPrefix = null) {
  if (!rawPath.startsWith('/')) throw new RequestHashError('request target path must be absolute');
  let path = rawPath;
  if (trustedProxyPrefix) {
    if (!trustedProxyPrefix.startsWith('/') || trustedProxyPrefix.endsWith('/')) {
      throw new RequestHashError("trusted proxy prefix must look like '/prefix'");
    }
    if (path === trustedProxyPrefix) path = '/';
    else if (path.startsWith(trustedProxyPrefix + '/')) path = path.slice(trustedProxyPrefix.length);
    else throw new RequestHashError('path does not carry the trusted proxy prefix');
  }
  return pctEncode(pctDecodeOnce(path), '/');
}

function utf8Compare(a, b) {
  const ab = Buffer.from(a, 'utf8');
  const bb = Buffer.from(b, 'utf8');
  const n = Math.min(ab.length, bb.length);
  for (let i = 0; i < n; i++) {
    if (ab[i] !== bb[i]) return ab[i] - bb[i];
  }
  return ab.length - bb.length;
}

export function canonicalQuery(rawQuery) {
  if (!rawQuery) return '';
  const pairs = [];
  for (const pair of rawQuery.split('&')) {
    const eq = pair.indexOf('=');
    const key = eq === -1 ? pair : pair.slice(0, eq);
    const value = eq === -1 ? null : pair.slice(eq + 1);
    pairs.push({
      key: pctDecodeOnce(key.replace(/\+/g, ' ')),
      value: value === null ? null : pctDecodeOnce(value.replace(/\+/g, ' ')),
    });
  }
  // Sort by (key UTF-8 bytes, value UTF-8 bytes, bare-key-first tie-break),
  // matching the locked reference exactly.
  pairs.sort((a, b) => {
    const byKey = utf8Compare(a.key, b.key);
    if (byKey !== 0) return byKey;
    const byValue = utf8Compare(a.value ?? '', b.value ?? '');
    if (byValue !== 0) return byValue;
    const bareA = a.value === null ? 0 : 1;
    const bareB = b.value === null ? 0 : 1;
    return bareA - bareB;
  });
  return pairs
    .map(pair => {
      const key = pctEncode(pair.key, '');
      return pair.value === null ? key : `${key}=${pctEncode(pair.value, '')}`;
    })
    .join('&');
}

export function sha256Hex(data) {
  return createHash('sha256').update(data).digest('hex');
}

// request_hash = SHA-256 hex of
// "<METHOD>\n<canonical_path>\n<canonical_query>\nSHA256_HEX(<raw_body_bytes>)".
export function requestHash(method, rawPath, rawQuery, body, trustedProxyPrefix = null) {
  const canonical = [
    method.toUpperCase(),
    canonicalPath(rawPath, trustedProxyPrefix),
    canonicalQuery(rawQuery),
    sha256Hex(body),
  ].join('\n');
  return sha256Hex(Buffer.from(canonical, 'utf8'));
}
