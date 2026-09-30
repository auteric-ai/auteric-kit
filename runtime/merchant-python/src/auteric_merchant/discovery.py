"""MEP/1 section 6 discovery trust: signed well-known document serving.

Wire format (v1): the control plane delivers a signed envelope

    {"document": {<UCP profile with store_id, hostname, issued_at, expires_at, ...>},
     "signature": {"alg": "EdDSA", "kid": "<key-id>", "value": "<base64url>"}}

where ``value`` is the Ed25519 signature over the JCS (RFC 8785) canonical
serialization of ``document`` — the whole document, not a partial payload.
The discovery key is pinned in the installation's trust bundle and is
distinct from the execution keys. The merchant NEVER signs the document
locally; it only verifies, caches (TTL <= 30s), and serves.

JCS subset implemented here: objects with sorted keys (UTF-16 code-unit
order), no whitespace, strings escaped per RFC 8785, integers plain, floats
formatted with the ECMAScript Number::toString shortest-round-trip
algorithm. Non-finite floats are rejected. That covers the full RFC 8785
data model, so this is a conforming serializer for JSON-compatible input.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CACHE_TTL_SECONDS = 30.0  # policy-change ceiling per MEP/1 section 6

_ESCAPE = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


class DiscoveryError(Exception):
    """Verification, freshness, or availability failure for the document."""


def _ecmascript_number(value: float) -> str:
    """ECMAScript Number::toString (shortest round-trip), per RFC 8785."""
    if not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid in JCS")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    repr_text = repr(abs(value))
    if "e" in repr_text or "E" in repr_text:
        mantissa_text, exponent_text = repr_text.lower().split("e")
        exponent = int(exponent_text)
    else:
        mantissa_text, exponent = repr_text, 0
    if "." in mantissa_text:
        int_part, frac_part = mantissa_text.split(".")
    else:
        int_part, frac_part = mantissa_text, ""
    combined = int_part + frac_part
    digits = combined.strip("0") or "0"
    # value = int(combined) * 10^(exponent - len(frac_part)); n positions the
    # decimal point so that value = 0.<digits> * 10^n.
    n = exponent - len(frac_part) + len(combined.lstrip("0"))
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
    e = n - 1
    return sign + mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))


def _jcs_string(value: str) -> str:
    out = ['"']
    for char in value:
        code = ord(char)
        if code in _ESCAPE:
            out.append(_ESCAPE[code])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    if isinstance(value, dict):
        # RFC 8785 sorts keys by UTF-16 code units.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_jcs_string(key) + ":" + _jcs(value[key]) for key in keys) + "}"
    raise TypeError(f"unsupported type for JCS: {type(value).__name__}")


def jcs_canonicalize(document: Any) -> bytes:
    """RFC 8785 canonical serialization of a JSON-compatible value."""
    return _jcs(document).encode("utf-8")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _parse_timestamp(value: Any, field: str) -> float:
    if not isinstance(value, str):
        raise DiscoveryError(f"document {field} missing or not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryError(f"document {field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class DiscoveryTrustBundle:
    """Pinned discovery keys (kid -> raw 32-byte Ed25519 public key).

    Discovery keys are configured separately from execution keys.
    """

    keys: dict[str, bytes]


def verify_signed_document(
    envelope_bytes: bytes,
    trust: DiscoveryTrustBundle,
    *,
    store_id: str,
    hostname: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify a signed discovery envelope; returns the document payload.

    Raises DiscoveryError on any failure: malformed envelope, unknown kid,
    bad signature, store/hostname mismatch, or expired document.
    """
    now = time.time() if now is None else now
    try:
        envelope = json.loads(envelope_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DiscoveryError("malformed discovery envelope") from exc
    if not isinstance(envelope, dict):
        raise DiscoveryError("malformed discovery envelope")
    document = envelope.get("document")
    signature = envelope.get("signature")
    if not isinstance(document, dict) or not isinstance(signature, dict):
        raise DiscoveryError("malformed discovery envelope")
    if signature.get("alg") != "EdDSA":
        raise DiscoveryError("unsupported discovery signature algorithm")
    kid = signature.get("kid")
    value = signature.get("value")
    if not isinstance(kid, str) or not isinstance(value, str):
        raise DiscoveryError("malformed discovery signature")
    raw_key = trust.keys.get(kid)
    if raw_key is None:
        raise DiscoveryError("unknown discovery key id")
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(
            _b64url_decode(value), jcs_canonicalize(document)
        )
    except (InvalidSignature, ValueError) as exc:
        raise DiscoveryError("invalid discovery signature") from exc

    if document.get("store_id") != store_id:
        raise DiscoveryError("document store_id mismatch")
    if document.get("hostname") != hostname:
        raise DiscoveryError("document hostname mismatch")
    _parse_timestamp(document.get("issued_at"), "issued_at")
    expires_at = _parse_timestamp(document.get("expires_at"), "expires_at")
    if now >= expires_at:
        raise DiscoveryError("discovery document expired")
    return document


class DiscoveryService:
    """Fetches, verifies, caches, and serves the signed discovery document.

    The fetcher is a pluggable async callable returning the signed envelope
    bytes (e.g. an httpx GET against the control plane, or a file read).
    The verified document is cached for at most 30 seconds. On fetch or
    verification failure, the last-known-valid document is served only while
    it is still within its own expires_at; afterwards serving fails closed.
    """

    def __init__(
        self,
        *,
        fetcher: Callable[[], Awaitable[bytes]],
        trust: DiscoveryTrustBundle,
        store_id: str,
        hostname: str,
        cache_ttl: float = CACHE_TTL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        if cache_ttl > CACHE_TTL_SECONDS:
            raise ValueError("discovery cache TTL must be <= 30 seconds")
        self._fetcher = fetcher
        self._trust = trust
        self._store_id = store_id
        self._hostname = hostname
        self._cache_ttl = cache_ttl
        self._now = now or time.time
        self._cached_bytes: bytes | None = None
        self._cached_at: float = 0.0
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_document_bytes(self) -> bytes:
        """Return the canonical bytes of the currently valid document."""
        now = self._now()
        if self._cached_bytes is not None and now < self._cached_at + self._cache_ttl:
            if now < self._expires_at:
                return self._cached_bytes
            raise DiscoveryError("cached discovery document expired")
        async with self._lock:
            now = self._now()
            if self._cached_bytes is not None and now < self._cached_at + self._cache_ttl:
                if now < self._expires_at:
                    return self._cached_bytes
                raise DiscoveryError("cached discovery document expired")
            try:
                envelope_bytes = await self._fetcher()
                document = verify_signed_document(
                    envelope_bytes,
                    self._trust,
                    store_id=self._store_id,
                    hostname=self._hostname,
                    now=now,
                )
            except DiscoveryError:
                if self._cached_bytes is not None and now < self._expires_at:
                    return self._cached_bytes
                raise
            except Exception:
                if self._cached_bytes is not None and now < self._expires_at:
                    return self._cached_bytes
                raise DiscoveryError("discovery fetch failed") from None
            self._cached_bytes = jcs_canonicalize(document)
            self._cached_at = now
            self._expires_at = _parse_timestamp(document.get("expires_at"), "expires_at")
            return self._cached_bytes
