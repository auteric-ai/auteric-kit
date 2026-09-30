"""MEP/1 section 3 request-hash canonicalization.

    request_hash = SHA-256 of "<METHOD>\n<canonical_path>\n<canonical_query>\nSHA256_HEX(<raw_body_bytes>)"

Rules (locked):
- METHOD uppercased.
- Path: as received on the wire, minus an installation-configured trusted
  proxy prefix only; single percent-decoding pass of non-unreserved bytes;
  decoded text NFC-normalized; re-encoded as UTF-8 percent-encoding with
  uppercase hex; unreserved (RFC 3986) characters stay literal. Trailing
  slash and "//" are significant and preserved.
- Query: no leading '?', split on '&', '+' decodes to space, each k/v
  decoded and re-encoded with the same rules, pairs sorted lexicographically
  by (k, v) as UTF-8 byte strings, repeated keys preserved, a bare key 'k'
  renders without '=' and differs from 'k='.
- Body: raw entity bytes as received (after transfer-decoding); empty body
  hashes as SHA-256 of the empty string.
"""
from __future__ import annotations

import hashlib
import unicodedata

_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX = b"0123456789ABCDEF"


class RequestHashError(ValueError):
    """The raw request target cannot be canonicalized (e.g. invalid UTF-8)."""


def _decode_component(raw: str, *, plus_as_space: bool) -> str:
    """One percent-decoding pass; returns the decoded unicode string.

    Literal non-ASCII characters in the raw component are treated as their
    UTF-8 bytes (RFC 3987-style request targets). Strings produced by
    decoding raw wire bytes with errors="surrogateescape" (as the framework
    drivers do) round-trip to the original bytes. Invalid percent triplets
    are left literal; invalid UTF-8 raises RequestHashError.
    """
    data = raw.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte == 0x25 and i + 2 < len(data) + 0 and i + 2 <= len(data) - 1:
            # '%' with two following characters
            try:
                hi = int(chr(data[i + 1]), 16)
                lo = int(chr(data[i + 2]), 16)
            except (ValueError, IndexError):
                out.append(byte)
                i += 1
                continue
            out.append(hi * 16 + lo)
            i += 3
        elif byte == 0x2B and plus_as_space:  # '+'
            out.append(0x20)
            i += 1
        else:
            out.append(byte)
            i += 1
    try:
        return bytes(out).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestHashError(f"invalid UTF-8 in request target: {exc}") from exc


def _encode_component(decoded: str, extra_literal: bytes = b"") -> str:
    """NFC-normalize and re-encode: unreserved (plus extra_literal) stay
    literal, everything else is %XX uppercase hex of the UTF-8 bytes."""
    normalized = unicodedata.normalize("NFC", decoded)
    try:
        data = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RequestHashError(f"invalid UTF-8 in request target: {exc}") from exc
    allowed = _UNRESERVED | frozenset(extra_literal)
    out = bytearray()
    for byte in data:
        if byte in allowed:
            out.append(byte)
        else:
            out.append(0x25)
            out.append(_HEX[byte >> 4])
            out.append(_HEX[byte & 0x0F])
    return out.decode("ascii")


def canonicalize_path(raw_path: str, trusted_proxy_prefix: str | None = None) -> str:
    """Canonicalize the raw request path per MEP/1 section 3.2."""
    path = raw_path
    if trusted_proxy_prefix:
        prefix = trusted_proxy_prefix
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix):]
    return _encode_component(_decode_component(path, plus_as_space=False), extra_literal=b"/")


def canonicalize_query(raw_query: str) -> str:
    """Canonicalize the raw query string (no leading '?') per MEP/1 section 3.3."""
    if not raw_query:
        return ""
    if raw_query.startswith("?"):
        raw_query = raw_query[1:]
    pairs: list[tuple[bytes, bytes | None, str, str | None]] = []
    for part in raw_query.split("&"):
        if "=" in part:
            raw_k, raw_v = part.split("=", 1)
            key = _decode_component(raw_k, plus_as_space=True)
            value: str | None = _decode_component(raw_v, plus_as_space=True)
        else:
            key = _decode_component(part, plus_as_space=True)
            value = None
        key_nfc = unicodedata.normalize("NFC", key)
        value_nfc = None if value is None else unicodedata.normalize("NFC", value)
        sort_v = b"" if value_nfc is None else value_nfc.encode("utf-8")
        pairs.append((key_nfc.encode("utf-8"), None if value_nfc is None else sort_v, key_nfc, value_nfc))
    pairs.sort(key=lambda p: (p[0], b"" if p[1] is None else p[1]))
    rendered = []
    for _kb, _vb, key, value in pairs:
        if value is None:
            rendered.append(_encode_component(key))
        else:
            rendered.append(_encode_component(key) + "=" + _encode_component(value))
    return "&".join(rendered)


def hash_input(method: str, canonical_path: str, canonical_query: str, body: bytes) -> bytes:
    body_hex = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{canonical_path}\n{canonical_query}\n{body_hex}".encode("utf-8")


def request_hash(
    method: str,
    raw_path: str,
    raw_query: str,
    body: bytes,
    trusted_proxy_prefix: str | None = None,
) -> str:
    """Compute the MEP/1 request hash, returned as ``sha256:<lowercase hex>``."""
    canonical_path = canonicalize_path(raw_path, trusted_proxy_prefix)
    canonical_query = canonicalize_query(raw_query)
    digest = hashlib.sha256(hash_input(method, canonical_path, canonical_query, body)).hexdigest()
    return f"sha256:{digest}"
