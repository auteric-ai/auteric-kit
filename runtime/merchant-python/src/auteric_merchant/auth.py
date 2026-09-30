"""MEP/1 section 2 execution-JWT verification (Ed25519, pinned trust bundle).

Implements the mandatory verification order of section 2.2 for the token
itself (steps 3-9 and 10). Steps that need the raw request (bounded parsing,
route match, request-hash) are orchestrated by ``runtime.MerchantRuntime``
and passed in here.

Hard rules:
- alg MUST be "EdDSA"; jku/x5u are always rejected; kid must resolve to a
  key in the pinned bundle. No JWKS URL is ever fetched.
- Clock skew is +/-5 seconds; the token window exp-iat must be <= 30s.
- jti replay is rejected via a NonceCache with TTL >= 60s.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import AutericError

MAX_TOKEN_BYTES = 16 * 1024
MAX_HEADER_BYTES = 4 * 1024
MAX_PAYLOAD_BYTES = 12 * 1024
CLOCK_SKEW_SECONDS = 5
MAX_TOKEN_WINDOW_SECONDS = 30
TOKEN_TYP = "auteric-exec+jwt"

REQUIRED_CLAIMS = (
    "iss",
    "aud",
    "sub",
    "store_id",
    "environment",
    "operation",
    "contract_version",
    "binding_digest",
    "action_id",
    "jti",
    "request_hash",
    "iat",
    "exp",
)


@dataclass(frozen=True)
class TrustBundle:
    """Pinned trust configuration distributed with the installation.

    issuer_allowlist: fixed HTTPS URLs of accepted Gateway issuers.
    keys: kid -> raw 32-byte Ed25519 public key.
    """

    issuer_allowlist: tuple[str, ...]
    keys: dict[str, bytes]

    def public_key(self, kid: str) -> Ed25519PublicKey | None:
        raw = self.keys.get(kid)
        if raw is None:
            return None
        return Ed25519PublicKey.from_public_bytes(raw)


@dataclass(frozen=True)
class OperationBinding:
    """The installed manifest entry for one operation."""

    method: str
    path: str  # route template, e.g. "/api/auteric/v1/carts/{cart_id}/items"
    contract_version: str
    binding_digest: str  # sha256:<hex> of adapter + dependencies as registered


@dataclass(frozen=True)
class Installation:
    """Local installation configuration the runtime verifies against."""

    installation_id: str
    store_id: str
    environment: str  # production | staging | sandbox | dev
    manifest: dict[str, OperationBinding]
    enabled: bool = True
    root_path: str | None = None  # trusted proxy prefix, from config only

    @property
    def expected_audience(self) -> str:
        return f"urn:auteric:installation:{self.installation_id}"


@dataclass(frozen=True)
class VerifiedContext:
    """Identity and execution metadata proven by the verified token.

    The raw request body is never a source of identity; identity comes from
    the verified ``sub`` claim only. ``pairwise_principal`` always carries
    the verified pairwise id; ``principal`` is upgraded by the runtime to the
    merchant-local principal once the PrincipalResolver has run.
    """

    principal: str  # pairwise id from verified sub; later the resolved merchant principal
    action_id: str
    jti: str
    operation: str
    contract_version: str
    installation_id: str
    store_id: str
    issuer: str
    pairwise_principal: str | None = None
    expected_revision: int | None = None  # bound from validated input, if present
    operator_test: bool = False  # signed owner-authorized Connection Test only


@runtime_checkable
class NonceCache(Protocol):
    """jti replay protection. Implementations must retain entries for at
    least 60 seconds (the token window is 30s)."""

    async def check_and_store(self, jti: str, ttl_seconds: float) -> bool:
        """Atomically record jti; True if it was fresh, False if replayed."""
        ...


class InMemoryNonceCache:
    """Process-local nonce cache for tests and single-node deployments."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        if ttl_seconds < 60:
            raise ValueError("nonce cache TTL must be >= 60 seconds")
        self._ttl = ttl_seconds
        self._entries: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check_and_store(self, jti: str, ttl_seconds: float) -> bool:
        now = time.monotonic()
        expiry = now + max(ttl_seconds, self._ttl)
        async with self._lock:
            expired = [k for k, exp in self._entries.items() if exp <= now]
            for k in expired:
                del self._entries[k]
            if jti in self._entries:
                return False
            self._entries[jti] = expiry
            return True


def _b64url_decode(segment: str) -> bytes:
    if not segment or "=" in segment:
        raise _auth_failed("malformed token encoding")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError) as exc:
        raise _auth_failed("malformed token encoding") from exc


def _auth_failed(message: str) -> AutericError:
    return AutericError("UNAUTHENTICATED", message)


def _decode_json(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _auth_failed(f"malformed {what}") from exc
    if not isinstance(value, dict):
        raise _auth_failed(f"malformed {what}")
    return value


class TokenVerifier:
    """Verifies Signed Execution JWTs against a pinned trust bundle."""

    def __init__(
        self,
        installation: Installation,
        trust: TrustBundle,
        nonce_cache: NonceCache,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._installation = installation
        self._trust = trust
        self._nonce_cache = nonce_cache
        self._now = now or time.time

    async def verify(self, token: str, *, expected_request_hash: str) -> VerifiedContext:
        installation = self._installation

        # 2.2(1) bounded parsing of the token itself.
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise _auth_failed("token too large")
        parts = token.split(".")
        if len(parts) != 3:
            raise _auth_failed("malformed token")
        raw_header, raw_payload, raw_sig = parts
        if len(raw_header.encode("utf-8")) > MAX_HEADER_BYTES:
            raise _auth_failed("token header too large")
        if len(raw_payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise _auth_failed("token payload too large")

        header = _decode_json(_b64url_decode(raw_header), "token header")
        payload = _decode_json(_b64url_decode(raw_payload), "token payload")
        signature = _b64url_decode(raw_sig)

        # Header policy: EdDSA only, pinned kid, jku/x5u always rejected.
        if header.get("alg") != "EdDSA":
            raise _auth_failed("unsupported alg")
        if header.get("typ") != TOKEN_TYP:
            raise _auth_failed("unexpected typ")
        if "jku" in header or "x5u" in header:
            raise _auth_failed("external key references are not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise _auth_failed("missing kid")

        for claim in REQUIRED_CLAIMS:
            if claim not in payload:
                raise _auth_failed(f"missing claim {claim}")

        # 2.2(3) iss allowlist -> kid in pinned bundle -> Ed25519 signature.
        iss = payload["iss"]
        if not isinstance(iss, str) or iss not in self._trust.issuer_allowlist:
            raise _auth_failed("issuer not allowed")
        public_key = self._trust.public_key(kid)
        if public_key is None:
            raise _auth_failed("unknown key id")
        try:
            public_key.verify(signature, f"{raw_header}.{raw_payload}".encode("ascii"))
        except InvalidSignature as exc:
            raise _auth_failed("invalid signature") from exc

        # 2.2(4) temporal validity with <= 5s skew; window <= 30s.
        now = self._now()
        iat, exp = payload["iat"], payload["exp"]
        if not isinstance(iat, (int, float)) or isinstance(iat, bool):
            raise _auth_failed("invalid iat")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise _auth_failed("invalid exp")
        if exp - iat > MAX_TOKEN_WINDOW_SECONDS:
            raise _auth_failed("token window exceeds 30 seconds")
        if now > exp + CLOCK_SKEW_SECONDS:
            raise _auth_failed("token expired")
        if iat > now + CLOCK_SKEW_SECONDS:
            raise _auth_failed("token issued in the future")

        # 2.2(5) audience / store / environment bind to this installation.
        if payload["aud"] != installation.expected_audience:
            raise _auth_failed("audience mismatch")
        if payload["store_id"] != installation.store_id:
            raise _auth_failed("store mismatch")
        if payload["environment"] != installation.environment:
            raise _auth_failed("environment mismatch")

        # 2.2(6) operation must be in the installed manifest; the route
        # (method/path) match itself happens in the runtime against the
        # canonical path.
        operation = payload["operation"]
        if not isinstance(operation, str) or operation not in installation.manifest:
            raise AutericError("RESOURCE_NOT_FOUND", "unknown operation")
        binding = installation.manifest[operation]
        if payload["contract_version"] != binding.contract_version:
            raise AutericError("CONTRACT_MISMATCH", "contract version mismatch")

        # 2.2(7) request hash per section 3 (computed by the runtime).
        if payload["request_hash"] != expected_request_hash:
            raise _auth_failed("request hash mismatch")

        # 2.2(8) binding digest must match the current registration.
        if payload["binding_digest"] != binding.binding_digest:
            raise AutericError("CONTRACT_MISMATCH", "binding digest mismatch")

        # 2.2(9) jti replay rejection (nonce cache >= 60s).
        jti = payload["jti"]
        if not isinstance(jti, str) or not jti:
            raise _auth_failed("invalid jti")
        fresh = await self._nonce_cache.check_and_store(jti, ttl_seconds=60.0)
        if not fresh:
            raise _auth_failed("replay detected")

        # 2.2(10) installation must be enabled and not revoked.
        if not installation.enabled:
            raise AutericError("FORBIDDEN", "installation disabled")

        sub = payload["sub"]
        if not isinstance(sub, str) or not sub:
            raise _auth_failed("invalid sub")
        operator_test = payload.get("operator_test", False)
        if not isinstance(operator_test, bool):
            raise _auth_failed("invalid operator_test")

        return VerifiedContext(
            principal=sub,
            action_id=str(payload["action_id"]),
            jti=jti,
            operation=operation,
            contract_version=str(payload["contract_version"]),
            installation_id=installation.installation_id,
            store_id=installation.store_id,
            issuer=iss,
            pairwise_principal=sub,
            operator_test=operator_test,
        )
