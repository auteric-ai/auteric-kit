"""MEP/1 §3 request-hash canonicalization.

Mirrors the locked reference implementation in
packages/commerce-contracts/tools/gen_request_hash_vectors.py; the golden
vectors in packages/commerce-contracts/vectors/request-hash.json are the
conformance test for this module.
"""

import hashlib
import unicodedata

UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
HEXDIGITS = frozenset("0123456789abcdefABCDEF")


class RequestHashError(ValueError):
    """Raised when a raw request component is not hashable under MEP/1 §3."""


def _pct_decode_once(raw: str) -> str:
    """Decode every %XX triplet exactly once; literal text passes through as UTF-8."""
    data = raw.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x25:  # '%'
            if i + 2 >= len(data):
                raise RequestHashError("truncated percent-encoding")
            pair = chr(data[i + 1]) + chr(data[i + 2])
            if pair[0] not in HEXDIGITS or pair[1] not in HEXDIGITS:
                raise RequestHashError(f"invalid percent-encoding %{pair}")
            out.append(int(pair, 16))
            i += 3
        else:
            out.append(data[i])
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestHashError(f"path/query is not valid UTF-8: {exc}") from exc


def _pct_encode(decoded: str, extra_literals: str) -> str:
    """NFC-normalize and re-encode: unreserved + extra_literals stay literal."""
    normalized = unicodedata.normalize("NFC", decoded)
    out = []
    for char in normalized:
        if char in UNRESERVED or char in extra_literals:
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out)


def canonical_path(raw_path: str, trusted_proxy_prefix: str | None = None) -> str:
    if not raw_path.startswith("/"):
        raise RequestHashError("request target path must be absolute")
    path = raw_path
    if trusted_proxy_prefix:
        if not trusted_proxy_prefix.startswith("/") or trusted_proxy_prefix.endswith("/"):
            raise RequestHashError("trusted proxy prefix must look like '/prefix'")
        if path == trusted_proxy_prefix:
            path = "/"
        elif path.startswith(trusted_proxy_prefix + "/"):
            path = path[len(trusted_proxy_prefix):]
        else:
            raise RequestHashError("path does not carry the trusted proxy prefix")
    return _pct_encode(_pct_decode_once(path), extra_literals="/")


def canonical_query(raw_query: str) -> str:
    if not raw_query:
        return ""
    pairs = []
    for pair in raw_query.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
        else:
            key, value = pair, None
        key_dec = _pct_decode_once(key.replace("+", " "))
        value_dec = None if value is None else _pct_decode_once(value.replace("+", " "))
        pairs.append((key_dec, value_dec))
    pairs.sort(key=lambda kv: (
        kv[0].encode("utf-8"),
        b"" if kv[1] is None else kv[1].encode("utf-8"),
        0 if kv[1] is None else 1,
    ))
    rendered = []
    for key_dec, value_dec in pairs:
        key_enc = _pct_encode(key_dec, extra_literals="")
        if value_dec is None:
            rendered.append(key_enc)
        else:
            rendered.append(key_enc + "=" + _pct_encode(value_dec, extra_literals=""))
    return "&".join(rendered)


def request_hash(method: str, raw_path: str, raw_query: str, body: bytes,
                 trusted_proxy_prefix: str | None = None) -> str:
    canonical = "\n".join([
        method.upper(),
        canonical_path(raw_path, trusted_proxy_prefix),
        canonical_query(raw_query),
        hashlib.sha256(body).hexdigest(),
    ])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
