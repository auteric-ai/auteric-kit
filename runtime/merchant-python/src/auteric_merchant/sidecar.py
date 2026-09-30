"""Deterministic, profile-driven Merchant Runtime sidecar for MEP/1.

The sidecar is deliberately *not* a proxy: it only installs profiles for
operations already present in the locked MEP manifest, and it receives a
fully verified canonical invocation through :class:`MerchantRuntime` before
it can contact a merchant service.  Profiles contain references to secrets,
never the secrets themselves.  Discovery/CI may create candidate profiles;
this module executes only profiles marked verified and enabled.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from http.cookies import SimpleCookie
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import quote, urlencode, urljoin, urlsplit

from .auth import Installation, TrustBundle, VerifiedContext
from .errors import AutericError, ERROR_CODES
from .runtime import MerchantRuntime
from .contracts_generated import MERCHANT_PROTOCOL, OPERATIONS, REGISTRY_DIGEST, REGISTRY_VERSION
from . import contracts_generated as contracts
from .sidecar_ops import AuditEvent, AuditSink, InMemoryAuditSink, LocalPolicySnapshot


INTEGRATION_MODES = frozenset({"platform_connector", "existing_internal_api", "service_bridge", "thin_bridge"})


@dataclass(frozen=True)
class AdapterCandidate:
    """A discovered integration option; discovery never selects or enables it."""
    candidate_id: str
    operation: str
    mode: str
    source: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.operation or not self.source:
            raise ValueError("adapter candidate requires id, operation, and source")
        if self.mode not in INTEGRATION_MODES:
            raise ValueError("unsupported adapter integration mode")


@dataclass(frozen=True)
class MerchantAdapterSelection:
    """An explicit merchant choice, retained before an adapter is generated."""
    operation: str
    candidate_id: str
    selected_by: str
    selection_id: str = ""
    store_id: str = ""
    environment: str = ""
    selected_at: float = 0.0

    def __post_init__(self) -> None:
        if not all((self.operation, self.candidate_id, self.selected_by, self.selection_id,
                    self.store_id, self.environment)) or self.selected_at <= 0:
            raise ValueError("merchant adapter selection requires an authenticated selection record")


def resolve_merchant_selection(candidates: list[AdapterCandidate], selection: MerchantAdapterSelection) -> AdapterCandidate:
    """Resolve a choice exactly; ambiguous discovery can never pick a fallback."""
    matches = [candidate for candidate in candidates if candidate.candidate_id == selection.candidate_id]
    if len(matches) != 1 or matches[0].operation != selection.operation:
        raise ValueError("merchant selection does not resolve to one candidate for the canonical operation")
    return matches[0]


@dataclass(frozen=True)
class HttpTarget:
    """A pinned merchant target; ``base_url`` must be configured, never input."""
    base_url: str
    method: str
    path: str
    timeout_seconds: float = 10.0
    retries: int = 0
    max_response_bytes: int = 4 * 1024 * 1024
    auth_scheme: str = "none"  # none | bearer | api_key
    credential_ref: str | None = None
    credential_header: str = "authorization"
    mtls_cert_ref: str | None = None
    mtls_key_ref: str | None = None
    request_hmac_secret_ref: str | None = None
    response_hmac_secret_ref: str | None = None
    cookie_session: bool = False
    csrf_header: str | None = None
    csrf_secret_ref: str | None = None
    csrf_cookie_name: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("sidecar target must be an absolute bare HTTP(S) origin")
        if not self.path.startswith("/") or "//" in self.path:
            raise ValueError("sidecar target path must be an absolute normalized path")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("sidecar timeout must be between 0 and 60 seconds")
        if self.retries < 0 or self.retries > 3:
            raise ValueError("sidecar retries must be between 0 and 3")
        if self.method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("unsupported merchant HTTP method")
        if not 1 <= self.max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("sidecar response limit must be between 1 byte and 4 MiB")
        if self.auth_scheme not in {"none", "bearer", "api_key"}:
            raise ValueError("unsupported merchant authentication scheme")
        if self.auth_scheme != "none" and not self.credential_ref:
            raise ValueError("merchant authentication requires a credential reference")
        if self.credential_ref and self.auth_scheme == "none":
            raise ValueError("credential reference requires an authentication scheme")
        if self.csrf_header and bool(self.csrf_secret_ref) == bool(self.csrf_cookie_name):
            raise ValueError("CSRF header requires exactly one secret or session-cookie source")
        if self.csrf_cookie_name and not self.cookie_session:
            raise ValueError("CSRF cookie source requires cookie_session")
        if bool(self.mtls_cert_ref) != bool(self.mtls_key_ref):
            raise ValueError("mTLS requires both certificate and key references")


@dataclass(frozen=True)
class VerificationEvidence:
    """Immutable proof that one exact mapping passed named tests in one environment."""
    evidence_id: str
    operation: str
    environment: str
    mapping_fingerprint: str
    contract_fingerprint: str
    passed_at: float
    expires_at: float
    test_suites: tuple[str, ...]
    payment_handler_verified: bool = False

    def __post_init__(self) -> None:
        if not all((self.evidence_id, self.operation, self.environment, self.test_suites)):
            raise ValueError("verification evidence is incomplete")
        if self.passed_at <= 0 or self.expires_at <= self.passed_at:
            raise ValueError("verification evidence has an invalid validity window")
        if not self.mapping_fingerprint.startswith("sha256:") or not self.contract_fingerprint.startswith("sha256:"):
            raise ValueError("verification evidence fingerprints must be sha256 digests")


@dataclass(frozen=True)
class ExecutionProfile:
    """Versioned deterministic instructions for exactly one canonical action."""
    operation: str
    integration_version: str
    mapping_fingerprint: str
    contract_fingerprint: str
    target: HttpTarget
    request_body: Mapping[str, str] = field(default_factory=dict)
    request_query: Mapping[str, str] = field(default_factory=dict)
    request_headers: Mapping[str, str] = field(default_factory=dict)
    response_mode: str = "identity"  # identity | field_map
    response_fields: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = False
    verification: VerificationEvidence | None = None
    integration_mode: str = "existing_internal_api"
    merchant_selection_id: str | None = None
    reconciliation_target: HttpTarget | None = None

    def __post_init__(self) -> None:
        if not self.operation or not self.integration_version:
            raise ValueError("profile requires operation and integration version")
        if not self.mapping_fingerprint.startswith("sha256:") or not self.contract_fingerprint.startswith("sha256:"):
            raise ValueError("profile fingerprints must be sha256 digests")
        if self.response_mode not in {"identity", "field_map"}:
            raise ValueError("unsupported response transformation")
        if self.response_mode == "field_map" and not self.response_fields:
            raise ValueError("field_map response transformation needs fields")
        if self.enabled and (self.verification is None or not self.merchant_selection_id):
            raise ValueError("an enabled profile needs merchant selection and verification evidence")
        if self.verification is not None and (
            self.verification.operation != self.operation
            or self.verification.mapping_fingerprint != self.mapping_fingerprint
            or self.verification.contract_fingerprint != self.contract_fingerprint
        ):
            raise ValueError("verification evidence does not bind to this execution profile")
        if (
            self.operation == "complete_checkout"
            and self.enabled
            and not self.verification.payment_handler_verified
        ):
            raise ValueError("complete_checkout cannot be enabled without verified payment evidence")
        if self.integration_mode not in INTEGRATION_MODES:
            raise ValueError("unsupported adapter integration mode")
        if self.reconciliation_target is not None and self.reconciliation_target.method.upper() != "GET":
            raise ValueError("reconciliation target must be an idempotent GET lookup")


SecretResolver = Callable[[str], str]
Sender = Callable[[str, str, Mapping[str, str], bytes, float, tuple[str, str] | None], Awaitable[tuple[int, Mapping[str, str], bytes]]]


class SessionStore(Protocol):
    def load(self, installation_id: str, principal: str, operation: str) -> dict[str, str]: ...
    def save(self, installation_id: str, principal: str, operation: str, cookies: Mapping[str, str]) -> None: ...


class InMemorySessionStore:
    """Principal-isolated cookie storage for one-process deployments and tests."""
    def __init__(self) -> None:
        self._cookies: dict[tuple[str, str, str], dict[str, str]] = {}

    def load(self, installation_id: str, principal: str, operation: str) -> dict[str, str]:
        return dict(self._cookies.get((installation_id, principal, operation), {}))

    def save(self, installation_id: str, principal: str, operation: str, cookies: Mapping[str, str]) -> None:
        self._cookies[(installation_id, principal, operation)] = dict(cookies)


def _fingerprint(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lookup(source: str, *, input_data: Mapping[str, Any], path_params: Mapping[str, str], ctx: VerifiedContext) -> Any:
    """Resolve only fixed profile expressions; no eval, templates, or request URLs."""
    if source.startswith("input."):
        return input_data.get(source[6:])
    if source.startswith("path."):
        return path_params.get(source[5:])
    if source == "context.principal":
        return ctx.principal
    if source == "context.action_id":
        return ctx.action_id
    raise AutericError("UPSTREAM_ERROR", "invalid verified execution profile", action_id=ctx.action_id)


def _json_path(value: Any, path: str) -> Any:
    if path == "$":
        return value
    if not path.startswith("$."):
        raise ValueError("response paths must start with $.")
    for key in path[2:].split("."):
        if not isinstance(value, Mapping) or key not in value:
            raise KeyError(path)
        value = value[key]
    return value


class MerchantSidecar:
    """Runs verified profiles behind the existing MEP/1 runtime.

    ``health`` and ``audit_events`` deliberately contain fingerprints and
    outcome categories only; bodies, cookies, authorization headers and secret
    values never enter sidecar telemetry.
    """

    def __init__(self, *, installation: Installation, trust: TrustBundle,
                 profiles: Mapping[str, ExecutionProfile], secret_resolver: SecretResolver,
                 sender: Sender | None = None, principal_resolver=None, execution_store=None,
                 session_store: SessionStore | None = None, audit_sink: AuditSink | None = None,
                 policy_snapshot: LocalPolicySnapshot | None = None, durable: bool = False,
                 storage_mode: str = "durable") -> None:
        self.profiles = dict(profiles)
        self.secret_resolver = secret_resolver
        self.sender = sender or self._httpx_sender
        self.session_store = session_store or InMemorySessionStore()
        self.audit_sink = audit_sink or InMemoryAuditSink()
        self.audit_events = self.audit_sink.events if isinstance(self.audit_sink, InMemoryAuditSink) else []
        self.policy_snapshot = policy_snapshot
        self.durable = durable
        self.storage_mode = storage_mode
        self.execution_store = execution_store
        self._circuits: dict[str, tuple[int, float | None]] = {}
        adapters = {}
        for operation, profile in self.profiles.items():
            if operation not in OPERATIONS:
                raise ValueError("profile operation is not in the locked registry")
            if operation != profile.operation or operation not in installation.manifest:
                raise ValueError("profiles must correspond to manifest-listed canonical operations")
            binding = installation.manifest[operation]
            if profile.contract_fingerprint != binding.binding_digest:
                raise ValueError("profile contract fingerprint does not match installed binding")
            if profile.enabled:
                assert profile.verification is not None
                if profile.verification.environment != installation.environment:
                    raise ValueError("verification evidence is for another environment")
                if profile.verification.expires_at <= time.time():
                    raise ValueError("verification evidence has expired")
            # Disabled profiles are installed only so a signed, owner-started
            # Connection Test can exercise the exact generated mapping. The
            # adapter itself remains fail-closed for every ordinary token.
            adapters[operation] = self._adapter(profile)
        self.runtime = MerchantRuntime(installation=installation, trust=trust, adapters=adapters,
                                       principal_resolver=principal_resolver, execution_store=execution_store,
                                       nonce_cache=execution_store if hasattr(execution_store, "check_and_store") else None,
                                       health_handler=self.health)
        self.execution_store = self.runtime.execution_store
        self.installation = installation

    @classmethod
    def profile_from_dict(cls, raw: Mapping[str, Any]) -> ExecutionProfile:
        """Strict manifest loader used by installers/CI (JSON is YAML-compatible)."""
        allowed = {"operation", "integration_version", "mapping_fingerprint", "contract_fingerprint", "target", "request_body", "request_query", "request_headers", "response_mode", "response_fields", "enabled", "verification", "integration_mode", "merchant_selection_id", "reconciliation_target"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown execution-profile fields: {sorted(unknown)}")
        target = HttpTarget(**dict(raw["target"]))
        values = {k: v for k, v in raw.items() if k != "target"}
        if values.get("verification") is not None:
            values["verification"] = VerificationEvidence(**dict(values["verification"]))
        if values.get("reconciliation_target") is not None:
            values["reconciliation_target"] = HttpTarget(**dict(values["reconciliation_target"]))
        return ExecutionProfile(target=target, **values)

    def _adapter(self, profile: ExecutionProfile):
        async def invoke(ctx: VerifiedContext, input_data: dict[str, Any], path_params: dict[str, str]) -> Any:
            return await self.execute_profile(profile, ctx, input_data, path_params)
        return invoke

    async def execute_profile(self, profile: ExecutionProfile, ctx: VerifiedContext,
                              input_data: Mapping[str, Any], path_params: Mapping[str, str]) -> Any:
        bootstrap = ctx.operator_test and bool(profile.merchant_selection_id)
        if (not profile.enabled or profile.verification is None) and not bootstrap:
            raise AutericError("CAPABILITY_DISABLED", "canonical action is not enabled", action_id=ctx.action_id)
        if not bootstrap and profile.verification.expires_at <= time.time():
            raise AutericError("CAPABILITY_DISABLED", "verification evidence has expired", action_id=ctx.action_id)
        if self.policy_snapshot is not None and not self.policy_snapshot.allows(profile.operation) and not bootstrap:
            raise AutericError("CAPABILITY_DISABLED", "local policy snapshot is expired or denies this operation", action_id=ctx.action_id)
        failures, opened = self._circuits.get(profile.operation, (0, None))
        if opened and time.monotonic() - opened < 30:
            raise AutericError("UPSTREAM_ERROR", "merchant integration circuit is open", action_id=ctx.action_id)
        path = profile.target.path
        for key, value in path_params.items():
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        if "{" in path:
            raise AutericError("UPSTREAM_ERROR", "profile path parameter missing", action_id=ctx.action_id)
        query = {
            key: _lookup(source, input_data=input_data, path_params=path_params, ctx=ctx)
            for key, source in profile.request_query.items()
        }
        if profile.integration_mode == "service_bridge":
            payload = {
                "context": {
                    "principal": ctx.principal,
                    "subject": ctx.pairwise_principal or ctx.principal,
                    "actionId": ctx.action_id,
                    "jti": ctx.jti,
                    "operation": ctx.operation,
                    "contractVersion": ctx.contract_version,
                    "installationId": ctx.installation_id,
                    "pathParams": dict(path_params),
                    **({"expectedRevision": ctx.expected_revision} if ctx.expected_revision is not None else {}),
                },
                "input": dict(input_data),
            }
            # HTTP clients and intermediaries commonly discard GET bodies.
            # Preserve canonical read input in the private bridge query string
            # while mutations continue to carry the signed envelope body.
            if profile.target.method.upper() == "GET":
                query.update(dict(input_data))
                payload = {}
        else:
            payload = {out: _lookup(source, input_data=input_data, path_params=path_params, ctx=ctx)
                       for out, source in profile.request_body.items()}
        target_url = urljoin(profile.target.base_url, path)
        if query:
            target_url += "?" + urlencode(query, doseq=True)
        body = b"" if profile.target.method.upper() in {"GET", "DELETE"} and not payload else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"accept": "application/json", "x-auteric-action-id": ctx.action_id,
                   "x-auteric-correlation-id": ctx.jti}
        if profile.integration_mode == "service_bridge":
            # GET/DELETE bridge calls deliberately carry no body, so the
            # verified pairwise subject must travel in a sidecar-owned header.
            # Mutations retain the full language-neutral envelope, while the
            # same header gives bridges one consistent authenticated fallback.
            headers["x-auteric-subject"] = ctx.pairwise_principal or ctx.principal
        if body:
            headers["content-type"] = "application/json"
        for key, source in profile.request_headers.items():
            lowered = key.lower()
            if lowered in {"authorization", "cookie", "host", "x-auteric-action-id"}:
                raise AutericError("UPSTREAM_ERROR", "profile attempts to override a protected header", action_id=ctx.action_id)
            headers[lowered] = str(_lookup(source, input_data=input_data, path_params=path_params, ctx=ctx))
        if profile.target.credential_ref:
            credential = self.secret_resolver(profile.target.credential_ref)
            headers[profile.target.credential_header.lower()] = (
                "Bearer " + credential if profile.target.auth_scheme == "bearer" else credential
            )
        session_cookies: dict[str, str] = {}
        if profile.target.cookie_session:
            session_cookies = self.session_store.load(ctx.installation_id, ctx.principal, profile.operation)
            if session_cookies:
                headers["cookie"] = "; ".join(f"{key}={value}" for key, value in sorted(session_cookies.items()))
        if profile.target.csrf_header:
            if profile.target.csrf_cookie_name:
                csrf = session_cookies.get(profile.target.csrf_cookie_name)
                if not csrf:
                    raise AutericError("UPSTREAM_ERROR", "merchant CSRF session is not initialized", action_id=ctx.action_id)
            else:
                csrf = self.secret_resolver(profile.target.csrf_secret_ref or "")
            headers[profile.target.csrf_header.lower()] = csrf
        is_mutation = OPERATIONS[profile.operation]["side_effect"] == "write"
        if is_mutation:
            headers["idempotency-key"] = ctx.action_id
        if profile.target.request_hmac_secret_ref:
            secret = self.secret_resolver(profile.target.request_hmac_secret_ref).encode()
            timestamp = str(int(time.time()))
            body_digest = hashlib.sha256(body).hexdigest()
            signed = "\n".join((profile.target.method.upper(), urlsplit(target_url).path,
                                  timestamp, ctx.action_id, body_digest)).encode()
            headers["x-auteric-request-timestamp"] = timestamp
            headers["x-auteric-request-signature"] = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        cert = None
        if profile.target.mtls_cert_ref:
            cert = (self.secret_resolver(profile.target.mtls_cert_ref), self.secret_resolver(profile.target.mtls_key_ref or ""))
        attempts = 1 if is_mutation else profile.target.retries + 1
        try:
            for attempt in range(attempts):
                try:
                    status, response_headers, response_body = await self.sender(
                        profile.target.method.upper(), target_url, headers, body,
                        profile.target.timeout_seconds, cert,
                    )
                    if status not in {429, 502, 503, 504} or is_mutation or attempt + 1 == attempts:
                        break
                    await asyncio.sleep(min(0.05 * (2 ** attempt), 0.2))
                except (TimeoutError, OSError):
                    if is_mutation:
                        self._audit(ctx, profile, "UNCERTAIN_EXECUTION")
                        raise AutericError("EXECUTION_UNCERTAIN", "merchant outcome requires reconciliation", action_id=ctx.action_id)
                    if attempt + 1 == attempts:
                        raise
                    await asyncio.sleep(min(0.05 * (2 ** attempt), 0.2))
            else:  # pragma: no cover - loop always breaks or raises
                raise TimeoutError()
            if not 200 <= status < 300:
                self._failure(profile.operation)
                if is_mutation and status >= 500:
                    self._audit(ctx, profile, "UNCERTAIN_EXECUTION")
                    raise AutericError("EXECUTION_UNCERTAIN", "merchant returned an ambiguous write failure", action_id=ctx.action_id)
                # Trust only known business codes whose HTTP status matches.
                # Never forward arbitrary merchant messages or diagnostic data.
                try:
                    error_body = json.loads(response_body)
                    error = error_body.get("error")
                    code = error.get("code") if isinstance(error, dict) else error_body.get("code")
                except (ValueError, AttributeError):
                    code = None
                if code in ERROR_CODES and ERROR_CODES[code][0] == status:
                    raise AutericError(code, "merchant rejected the action", action_id=ctx.action_id)
                raise AutericError("UPSTREAM_ERROR", "merchant API rejected execution", action_id=ctx.action_id)
            if len(response_body) > profile.target.max_response_bytes:
                code = "EXECUTION_UNCERTAIN" if is_mutation else "UPSTREAM_ERROR"
                raise AutericError(code, "merchant response exceeded configured limit", action_id=ctx.action_id)
            if profile.target.cookie_session:
                set_cookie = next((value for key, value in response_headers.items() if key.lower() == "set-cookie"), None)
                if set_cookie:
                    parsed = SimpleCookie()
                    parsed.load(set_cookie)
                    session_cookies.update({key: morsel.value for key, morsel in parsed.items()})
                    self.session_store.save(ctx.installation_id, ctx.principal, profile.operation, session_cookies)
            if profile.target.response_hmac_secret_ref:
                signed = ctx.action_id.encode() + b"\n" + response_body
                expected = hmac.new(self.secret_resolver(profile.target.response_hmac_secret_ref).encode(), signed, hashlib.sha256).hexdigest()
                supplied = next((value for key, value in response_headers.items()
                                 if key.lower() == "x-auteric-response-signature"), "")
                if not hmac.compare_digest(expected, supplied):
                    code = "EXECUTION_UNCERTAIN" if is_mutation else "UPSTREAM_ERROR"
                    raise AutericError(code, "merchant response signature invalid", action_id=ctx.action_id)
            decoded = json.loads(response_body)
            output = decoded if profile.response_mode == "identity" else {k: _json_path(decoded, v) for k, v in profile.response_fields.items()}
            self._circuits[profile.operation] = (0, None)
            self._audit(ctx, profile, "COMPLETED")
            return output
        except AutericError:
            raise
        except (TimeoutError, OSError, ValueError, json.JSONDecodeError, KeyError):
            self._failure(profile.operation)
            outcome = "UNCERTAIN_EXECUTION" if is_mutation else "MERCHANT_API_ERROR"
            self._audit(ctx, profile, outcome)
            code = "EXECUTION_UNCERTAIN" if is_mutation else "UPSTREAM_ERROR"
            raise AutericError(code, "merchant API execution failed", action_id=ctx.action_id)

    def _failure(self, operation: str) -> None:
        count, _ = self._circuits.get(operation, (0, None))
        count += 1
        self._circuits[operation] = (count, time.monotonic() if count >= 5 else None)

    def _audit(self, ctx: VerifiedContext, profile: ExecutionProfile, outcome: str) -> None:
        self.audit_sink.record(AuditEvent(
            occurred_at=time.time(), installation_id=ctx.installation_id,
            action_id=ctx.action_id, operation=profile.operation,
            mapping_fingerprint=profile.mapping_fingerprint, outcome=outcome,
        ))

    async def reconcile(self, action_id: str) -> Mapping[str, Any]:
        """Resolve one uncertain write by its original action id, never by replaying it."""
        record = await self.execution_store.get(self.installation.installation_id, action_id)
        if record is None or record.status != "uncertain":
            raise ValueError("reconciliation requires an uncertain execution record")
        profile = self.profiles[record.operation]
        target = profile.reconciliation_target
        if target is None:
            raise ValueError("operation has no verified reconciliation target")
        path = target.path.replace("{action_id}", quote(action_id, safe=""))
        if "{" in path:
            raise ValueError("reconciliation target has unresolved path parameters")
        headers = {"accept": "application/json", "x-auteric-action-id": action_id}
        if target.credential_ref:
            credential = self.secret_resolver(target.credential_ref)
            headers[target.credential_header.lower()] = "Bearer " + credential if target.auth_scheme == "bearer" else credential
        cert = None
        if target.mtls_cert_ref:
            cert = (self.secret_resolver(target.mtls_cert_ref), self.secret_resolver(target.mtls_key_ref or ""))
        status, response_headers, body = await self.sender(
            target.method.upper(), urljoin(target.base_url, path), headers, b"", target.timeout_seconds, cert
        )
        if status == 404:
            return {"status": "pending", "action_id": action_id}
        if not 200 <= status < 300 or len(body) > target.max_response_bytes:
            raise RuntimeError("reconciliation query failed")
        if target.response_hmac_secret_ref:
            expected = hmac.new(self.secret_resolver(target.response_hmac_secret_ref).encode(), action_id.encode() + b"\n" + body, hashlib.sha256).hexdigest()
            supplied = next((value for key, value in response_headers.items() if key.lower() == "x-auteric-response-signature"), "")
            if not hmac.compare_digest(expected, supplied):
                raise RuntimeError("reconciliation response signature invalid")
        result = json.loads(body)
        contracts.validate_output(record.operation, result)
        await self.execution_store.complete(self.installation.installation_id, action_id, result)
        self.audit_sink.record(AuditEvent(time.time(), self.installation.installation_id, action_id,
                                          record.operation, profile.mapping_fingerprint, "RECONCILED"))
        return {"status": "completed", "action_id": action_id, "result": result}

    def health(self) -> dict[str, Any]:
        return {"merchant_protocol": "1", "enabled_operations": sorted(k for k, v in self.profiles.items()
                    if v.enabled and v.verification is not None and v.verification.expires_at > time.time()),
                "durable": self.durable, "storage_mode": self.storage_mode,
                "policy": ({"fingerprint": self.policy_snapshot.fingerprint, "expires_at": self.policy_snapshot.expires_at,
                            "valid": self.policy_snapshot.expires_at > time.time(),
                            "covers_enabled_operations": all(
                                not profile.enabled or operation in self.policy_snapshot.allowed_operations
                                for operation, profile in self.profiles.items()
                            )} if self.policy_snapshot else None),
                "profiles": {k: {"verified": v.verification is not None, "mapping_fingerprint": v.mapping_fingerprint,
                                  "integration_mode": v.integration_mode,
                                  "merchant_selection_id": v.merchant_selection_id,
                                  "circuit_open": bool(self._circuits.get(k, (0, None))[1])} for k, v in self.profiles.items()}}

    async def _httpx_sender(self, method, url, headers, body, timeout, cert):
        try:
            import httpx
        except ImportError as exc:  # clear deployment error; core SDK stays light
            raise RuntimeError("install auteric-merchant[sidecar] to execute HTTP profiles") from exc
        try:
            async with httpx.AsyncClient(timeout=timeout, cert=cert, follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, url, headers=headers, content=body) as response:
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(payload) + len(chunk) > 4 * 1024 * 1024:
                            raise ValueError("merchant response exceeded the hard limit")
                        payload.extend(chunk)
                    return response.status_code, dict(response.headers), bytes(payload)
        except httpx.TimeoutException as exc:
            raise TimeoutError("merchant request timed out") from exc
        except httpx.RequestError as exc:
            raise OSError("merchant transport failed") from exc


@dataclass(frozen=True)
class MerchantIntegrationManifest:
    """Versioned, fail-closed sidecar configuration for one installation."""
    store_id: str
    installation_id: str
    environment: str
    integration_version: str
    profiles: Mapping[str, ExecutionProfile]
    merchant_protocol: str = MERCHANT_PROTOCOL
    registry_version: str = REGISTRY_VERSION
    registry_digest: str = REGISTRY_DIGEST

    def validate(self, installation: Installation) -> None:
        if self.merchant_protocol != MERCHANT_PROTOCOL:
            raise ValueError("merchant protocol version is incompatible")
        if self.registry_version != REGISTRY_VERSION or self.registry_digest != REGISTRY_DIGEST:
            raise ValueError("manifest registry does not match the locked runtime registry")
        if (self.store_id, self.installation_id, self.environment) != (
            installation.store_id, installation.installation_id, installation.environment
        ):
            raise ValueError("manifest identity does not match the installation")
        if not self.integration_version:
            raise ValueError("manifest integration version is required")
        if set(self.profiles) - set(installation.manifest):
            raise ValueError("manifest contains operations outside the installation")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MerchantIntegrationManifest":
        allowed = {"store_id", "installation_id", "environment", "integration_version",
                   "merchant_protocol", "registry_version", "registry_digest", "profiles"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown integration-manifest fields: {sorted(unknown)}")
        profiles = {
            operation: MerchantSidecar.profile_from_dict(profile)
            for operation, profile in dict(raw.get("profiles", {})).items()
        }
        return cls(**{**dict(raw), "profiles": profiles})
