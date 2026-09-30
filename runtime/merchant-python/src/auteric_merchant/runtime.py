"""MerchantRuntime: MEP/1 plumbing around merchant-written adapters.

The SDK owns validation, authentication, idempotency, and dispatch; the
merchant writes small adapters keyed by operation name. An adapter is an
async-or-sync callable:

    async def add_to_cart(ctx: VerifiedContext, input: dict, path_params: dict) -> dict

It returns the operation's output payload (validated against the output
schema before it hits the wire) or raises AutericError for a specific wire
code. Only manifest-listed operations dispatch, and only after full
verification; the raw body is never trusted for identity.

Verification pipeline (MEP/1 section 2.2):
  1. bounded parsing (1 MiB body cap, bounded token)
  3. pinned-bundle signature verification (auth.TokenVerifier)
  4. exp/iat with 5s skew
  5. aud/store/environment
  6. operation in manifest + method/path route match on the canonical path
  7. request_hash match (section 3)
  8. binding_digest match
  9. jti nonce
  10. installation enabled
  11. principal resolution
  12. idempotency on (installation, action_id) + request_hash
  13. input validation -> adapter -> output validation
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from urllib.parse import unquote_plus
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from . import contracts_generated as contracts
from .auth import (
    Installation,
    InMemoryNonceCache,
    NonceCache,
    TokenVerifier,
    TrustBundle,
    VerifiedContext,
)
from .errors import AutericError
from .execution_store import (
    ExecutionInProgress,
    ExecutionStore,
    IdempotencyConflict,
    InMemoryExecutionStore,
)
from .principal_resolver import PrincipalResolver
from .request_hash import RequestHashError, canonicalize_path, request_hash

MAX_BODY_BYTES = 1 * 1024 * 1024
_BODILESS_METHODS = {"GET", "HEAD", "DELETE"}

Adapter = Callable[[VerifiedContext, dict[str, Any], dict[str, str]], Any]


@dataclass(frozen=True)
class RawRequest:
    """A request as received on the wire, before framework normalization.

    path and raw_query_string should be reconstructed so that
    ``value.encode("utf-8", "surrogateescape")`` yields the original bytes;
    the framework drivers do this for you.
    """

    method: str
    path: str
    raw_query_string: str
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True)
class RawResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: Any) -> "RawResponse":
        return cls(
            status=status,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"content-type": "application/json", "x-content-type-options": "nosniff"},
        )


def match_route(template: str, canonical_path: str) -> dict[str, str] | None:
    """Bind a contract route template against a canonical path.

    Segment-exact: an encoded slash canonicalizes to a literal '/', changes
    the segment count, and therefore fails the match (per MEP/1 section 3).
    """
    template_segments = template.split("/")
    path_segments = canonical_path.split("/")
    if len(template_segments) != len(path_segments):
        return None
    params: dict[str, str] = {}
    for tseg, pseg in zip(template_segments, path_segments):
        if tseg.startswith("{") and tseg.endswith("}"):
            params[tseg[1:-1]] = pseg
        elif tseg != pseg:
            return None
    return params


async def _run_adapter(adapter: Adapter, ctx: VerifiedContext, input_data: dict, path_params: dict) -> Any:
    if inspect.iscoroutinefunction(adapter):
        return await adapter(ctx, input_data, path_params)
    try:
        from starlette.concurrency import run_in_threadpool
    except ImportError:
        return await asyncio.to_thread(adapter, ctx, input_data, path_params)
    return await run_in_threadpool(adapter, ctx, input_data, path_params)


def _coerce_query_value(value: str) -> Any:
    """Apply the narrow wire coercions used by the Node runtime.

    Query values arrive as text, whereas the locked schemas correctly model
    pagination and price filters as integers.  Only unambiguous integer and
    boolean spellings are coerced; every other value remains a string.
    """
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def input_from_query(raw_query_string: str) -> dict[str, Any]:
    """Decode a raw query string for bodiless contract operations.

    Repeated keys intentionally use the last value, matching the merchant
    Node SDK.  Request hashing still uses the raw query string; this parser is
    only for the already-authenticated, schema-validated adapter input.
    """
    output: dict[str, Any] = {}
    if not raw_query_string:
        return output
    for pair in raw_query_string.split("&"):
        key_raw, separator, value_raw = pair.partition("=")
        key = unquote_plus(key_raw)
        value = unquote_plus(value_raw if separator else "")
        output[key] = _coerce_query_value(value)
    return output


class MerchantRuntime:
    def __init__(
        self,
        *,
        installation: Installation,
        trust: TrustBundle,
        adapters: dict[str, Adapter] | None = None,
        execution_store: ExecutionStore | None = None,
        principal_resolver: PrincipalResolver | None = None,
        nonce_cache: NonceCache | None = None,
        max_body_bytes: int = MAX_BODY_BYTES,
        health_handler: Callable[[], Any] | None = None,
    ) -> None:
        self.installation = installation
        self.trust = trust
        self.adapters: dict[str, Adapter] = dict(adapters or {})
        unknown = set(self.adapters) - set(installation.manifest)
        if unknown:
            raise ValueError(
                f"adapters registered for operations not in the installation manifest: {sorted(unknown)}"
            )
        self.execution_store: ExecutionStore = execution_store or InMemoryExecutionStore()
        self.principal_resolver = principal_resolver
        self._nonce_cache = nonce_cache or InMemoryNonceCache()
        self._max_body_bytes = max_body_bytes
        self._health_handler = health_handler

    async def execute(self, request: RawRequest) -> RawResponse:
        try:
            return await self._execute(request)
        except AutericError as exc:
            return RawResponse.json(exc.http_status, exc.to_envelope())
        except Exception:
            # Never leak internals (MEP/1 section 5).
            return RawResponse.json(
                502, AutericError("UPSTREAM_ERROR", "internal error").to_envelope()
            )

    async def _execute(self, request: RawRequest) -> RawResponse:
        installation = self.installation

        # 2.2(1) bounded parsing.
        if len(request.body) > self._max_body_bytes:
            raise AutericError("INVALID_INPUT", "request body too large")

        authorization = request.header("authorization")
        if not authorization or not authorization.startswith("Bearer "):
            raise AutericError("UNAUTHENTICATED", "missing bearer token")
        token = authorization[len("Bearer "):].strip()

        # 2.2(2)/(3.2-3.3) canonical request hash from raw wire values; the
        # trusted proxy prefix comes from installation config only.
        try:
            canonical_path = canonicalize_path(request.path, installation.root_path)
            expected_hash = request_hash(
                request.method,
                request.path,
                request.raw_query_string,
                request.body,
                installation.root_path,
            )
        except RequestHashError as exc:
            raise AutericError("INVALID_INPUT", "request target cannot be canonicalized") from exc

        # 2.2(3)-(10) token verification.
        verifier = TokenVerifier(installation, self.trust, self._nonce_cache)
        ctx = await verifier.verify(token, expected_request_hash=expected_hash)

        # 2.2(6) route match: method and canonical path must match the
        # contract for the claimed operation.
        binding = installation.manifest[ctx.operation]
        path_params = match_route(binding.path, canonical_path)
        if request.method.upper() != binding.method or path_params is None:
            raise AutericError("RESOURCE_NOT_FOUND", "route mismatch", action_id=ctx.action_id)

        # ``health`` is a control-plane probe, not a merchant capability.  It
        # still traverses the complete pinned JWT/request-hash verification
        # above, but it must not enter the commerce registry, idempotency
        # ledger, principal resolver, or bridge adapter dispatch.
        if ctx.operation == "health" and self._health_handler is not None:
            result = self._health_handler()
            if inspect.isawaitable(result):
                result = await result
            return RawResponse.json(200, result)

        # 2.2(11) principal resolution (ownership is enforced by adapters
        # against the resolved principal).
        merchant_principal: str | None = None
        if self.principal_resolver is not None:
            merchant_principal = await self.principal_resolver.resolve(ctx)
            ctx = replace(ctx, principal=merchant_principal)

        # Parse and validate input before dispatch (and before consuming the
        # idempotency key, so a malformed attempt stays retryable).
        if request.body:
            try:
                input_data = json.loads(request.body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise AutericError("INVALID_INPUT", "body is not valid JSON", action_id=ctx.action_id) from exc
        elif request.method.upper() in _BODILESS_METHODS:
            input_data = input_from_query(request.raw_query_string)
        else:
            input_data = {}
        if not isinstance(input_data, dict):
            raise AutericError("INVALID_INPUT", "input must be a JSON object", action_id=ctx.action_id)
        try:
            contracts.validate_input(ctx.operation, input_data)
        except contracts.ContractValidationError as exc:
            raise AutericError(
                "INVALID_INPUT", f"input validation failed: {exc.category}", action_id=ctx.action_id
            ) from exc
        revision = input_data.get("expected_revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            ctx = replace(ctx, expected_revision=revision)

        # Bind the ledger to verified authority as well as wire bytes. A valid
        # token for another subject must never retrieve the first subject's
        # receipt. Versioning deliberately makes legacy unbound rows conflict
        # rather than upgrading their authority implicitly.
        import hashlib
        ledger_hash = "authority-v1:" + hashlib.sha256(json.dumps({
            "request_hash": expected_hash, "subject": ctx.pairwise_principal or ctx.principal,
            "principal": ctx.principal, "store": ctx.store_id, "issuer": ctx.issuer,
            "operation": ctx.operation, "operator_test": ctx.operator_test,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        # 2.2(12) idempotency on (installation, action_id) + authority binding.
        try:
            outcome = await self.execution_store.reserve(
                installation.installation_id, ctx.action_id, ledger_hash, ctx.operation
            )
        except IdempotencyConflict as exc:
            raise AutericError(
                "IDEMPOTENCY_CONFLICT", "same action_id with a different payload", action_id=ctx.action_id
            ) from exc
        except ExecutionInProgress as exc:
            raise AutericError(
                "EXECUTION_UNCERTAIN", "an attempt with this action_id is still in flight", action_id=ctx.action_id
            ) from exc
        if outcome.replay:
            # 2.3: same action_id with a stored result returns it without
            # running the handler again.
            return RawResponse.json(200, outcome.record.result)

        adapter = self.adapters.get(ctx.operation)
        if adapter is None:
            await self._release(ctx)
            raise AutericError(
                "CAPABILITY_DISABLED", "operation has no installed adapter", action_id=ctx.action_id
            )

        # 2.2(13) business handler, then output validation.
        try:
            result = await _run_adapter(adapter, ctx, input_data, path_params)
        except AutericError as exc:
            if exc.code == "EXECUTION_UNCERTAIN":
                await self.execution_store.mark_uncertain(installation.installation_id, ctx.action_id)
            else:
                await self._release(ctx)
            raise
        except asyncio.CancelledError:
            if self._is_write(ctx.operation):
                await self.execution_store.mark_uncertain(installation.installation_id, ctx.action_id)
                raise AutericError(
                    "EXECUTION_UNCERTAIN",
                    "execution was cancelled after the write may have started",
                    action_id=ctx.action_id,
                )
            await self._release(ctx)
            raise
        except Exception as exc:
            if self._is_write(ctx.operation):
                await self.execution_store.mark_uncertain(installation.installation_id, ctx.action_id)
                raise AutericError(
                    "EXECUTION_UNCERTAIN",
                    "write outcome requires reconciliation",
                    action_id=ctx.action_id,
                ) from exc
            await self._release(ctx)
            raise AutericError("UPSTREAM_ERROR", "adapter failed", action_id=ctx.action_id) from exc
        try:
            contracts.validate_output(ctx.operation, result)
        except contracts.ContractValidationError as exc:
            if self._is_write(ctx.operation):
                await self.execution_store.mark_uncertain(installation.installation_id, ctx.action_id)
                raise AutericError(
                    "EXECUTION_UNCERTAIN",
                    "write completed with an invalid response; reconcile by action_id",
                    action_id=ctx.action_id,
                ) from exc
            await self._release(ctx)
            raise AutericError(
                "UPSTREAM_ERROR", "adapter output failed contract validation", action_id=ctx.action_id
            ) from exc

        try:
            await self.execution_store.complete(installation.installation_id, ctx.action_id, result)
        except Exception as exc:
            if self._is_write(ctx.operation):
                # The merchant may have committed even though receipt storage
                # failed. Never release this reservation or report safe failure.
                try:
                    await self.execution_store.mark_uncertain(installation.installation_id, ctx.action_id)
                except Exception:
                    pass  # retained executing reservation still blocks replay
                raise AutericError("EXECUTION_UNCERTAIN", "receipt persistence failed; reconcile by action_id",
                                   action_id=ctx.action_id) from exc
            raise
        return RawResponse.json(200, result)

    async def _release(self, ctx: VerifiedContext) -> None:
        """Free the idempotency key after a failed attempt so a legitimate
        retry (same action_id, same payload, new jti) can run cleanly."""
        release = getattr(self.execution_store, "release", None)
        if release is not None:
            await release(self.installation.installation_id, ctx.action_id)

    @staticmethod
    def _is_write(operation: str) -> bool:
        return contracts.OPERATIONS[operation]["side_effect"] == "write"
