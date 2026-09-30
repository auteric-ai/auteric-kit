"""Native HTTP merchant transport implementing MEP/1.

Executes canonical operations directly against a merchant runtime over HTTPS
with an Ed25519-signed execution JWT, locked request-hash canonicalization,
connect-time SSRF validation, bounded responses and a per-installation circuit
breaker. Writes that time out are reported as `uncertain` and are never
retried blindly; an explicit retry reuses action_id + payload with a new jti.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..storage import encode
from .base import MerchantTransport, TransportAction, TransportResult
from .request_hash import request_hash

EXECUTION_WINDOW_SECONDS = 30
MAX_REDIRECTS = 1
CHECKOUT_OPERATIONS = {"create_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}
ERROR_HTTP = {
    "INVALID_INPUT": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "RESOURCE_NOT_FOUND": 404,
    "REVISION_CONFLICT": 409,
    "OUT_OF_STOCK": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "CAPABILITY_DISABLED": 403,
    "CONTRACT_MISMATCH": 421,
    "RATE_LIMITED": 429,
    "EXECUTION_UNCERTAIN": 504,
    "PAYMENT_PENDING": 202,
    "UPSTREAM_ERROR": 502,
    "SSRF_BLOCKED": 502,
}
DEV_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_contracts = None


def contracts():
    """Load the generated commerce-contract validators (registry 1.0.0)."""
    global _contracts
    if _contracts is None:
        try:
            import auteric_contracts as module
        except ImportError:
            root = str(
                Path(__file__).resolve().parents[3]
                / "packages" / "commerce-contracts" / "generated" / "python"
            )
            if root not in sys.path:
                sys.path.insert(0, root)
            import auteric_contracts as module
        _contracts = module
    return _contracts


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def build_execution_jwt(private_key: Ed25519PrivateKey, kid: str, claims: dict) -> str:
    header = {"alg": "EdDSA", "typ": "auteric-exec+jwt", "kid": kid}
    signing_input = _b64url(encode(header).encode()) + "." + _b64url(encode(claims).encode())
    return signing_input + "." + _b64url(private_key.sign(signing_input.encode()))


def decode_execution_jwt(token: str):
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("malformed execution token")
    header = json.loads(_b64url_decode(parts[0]))
    claims = json.loads(_b64url_decode(parts[1]))
    return header, claims, (parts[0] + "." + parts[1]).encode(), _b64url_decode(parts[2])


def verify_execution_jwt(token: str, public_key) -> dict:
    """Merchant-side verification of the signature envelope (MEP/1 §2.2 step 3)."""
    header, claims, signing_input, signature = decode_execution_jwt(token)
    if header.get("alg") != "EdDSA" or header.get("typ") != "auteric-exec+jwt":
        raise ValueError("unexpected execution token header")
    if "jku" in header or "x5u" in header:
        raise ValueError("external key references are always rejected")
    public_key.verify(signature, signing_input)
    return claims


def execution_claims(installation, action: TransportAction, request_hash_value: str,
                     jti: str, issuer: str, now: int | None = None) -> dict:
    """Claims exactly per MEP/1 §2.1; the validity window is locked at 30s."""
    iat = int(time.time()) if now is None else now
    claims = {
        "iss": issuer,
        "aud": "urn:auteric:installation:" + installation.id,
        "sub": action.principal,
        "store_id": installation.store_id,
        "environment": installation.environment,
        "operation": action.operation,
        "contract_version": action.contract_version,
        "binding_digest": action.binding_digest,
        "action_id": action.action_id,
        "jti": jti,
        "request_hash": request_hash_value,
        "iat": iat,
        "exp": iat + EXECUTION_WINDOW_SECONDS,
    }
    if action.operator_test:
        claims["operator_test"] = True
    return claims


class SsrFBlocked(Exception):
    """Target or redirect violates MEP/1 §4."""


class ResponseTooLarge(Exception):
    """Merchant response exceeded the 4 MiB cap."""


def validate_endpoint(endpoint: str, environment: str):
    """Registration-time endpoint policy; DNS is re-validated at connect time."""
    url = urlsplit(endpoint)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Installation endpoint must be a bare origin")
    if url.scheme == "http":
        if environment != "dev" or url.hostname not in DEV_LOOPBACK_HOSTS:
            raise ValueError("Plain HTTP endpoints are limited to explicit dev loopback installations")
    elif url.scheme != "https":
        raise ValueError("Installation endpoint scheme must be https (http loopback in dev only)")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        return url  # Domain name; resolved and re-checked at every connect.
    if environment == "dev" and address.is_loopback and url.hostname in DEV_LOOPBACK_HOSTS:
        return url
    if not address.is_global:
        raise ValueError("Installation endpoint IP is not a public address")
    return url


def _operation_class(operation: str, side_effect: str) -> str:
    if side_effect == "read":
        return "read"
    return "checkout" if operation in CHECKOUT_OPERATIONS else "write"


def _build_request(operation: str, data: dict):
    """Bind path variables from the input; GET remainder becomes the query.

    Returns (method, raw_path, raw_query, body_bytes) exactly as they go on the
    wire, so the request hash covers the identical bytes.
    """
    contract = contracts().OPERATIONS[operation]
    method = contract["method"]
    remaining = dict(data)

    def substitute(match):
        name = match.group(1)
        if name not in remaining:
            raise ValueError(f"missing path parameter {name}")
        return quote(str(remaining.pop(name)), safe="")

    raw_path = re.sub(r"\{([a-z_]+)\}", substitute, contract["path"])
    # Null-valued optional fields (e.g. legacy model_dump output) are omitted;
    # null is never schema-valid, and a required null still fails as missing.
    remaining = {key: value for key, value in remaining.items() if value is not None}
    raw_query = ""
    body = b""
    if method == "GET":
        pairs = []
        for key in sorted(remaining):
            value = remaining[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif not isinstance(value, str):
                value = encode(value)
            pairs.append(quote_plus(key) + "=" + quote_plus(value))
        raw_query = "&".join(pairs)
    else:
        body = encode(remaining).encode()
    return method, raw_path, raw_query, body, remaining


def gateway_signing_keys(development: bool):
    """Gateway Ed25519 execution keyring: env seed, ephemeral in development.

    Returns {"default": (kid, Ed25519PrivateKey)} or None when no production
    key is configured (native dispatch then fails closed).
    """
    seed = os.environ.get("AUTERIC_EXECUTION_ED25519_SEED", "").strip()
    if seed:
        key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(seed))
    elif development:
        key = Ed25519PrivateKey.generate()
    else:
        return None
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"default": ("gw-" + hashlib.sha256(public).hexdigest()[:16], key)}


class NativeHttpTransport(MerchantTransport):
    def __init__(self, keyring=None, *, dbs=None, issuer="https://gateway.example.invalid",
                 timeouts=None, connect_timeout=5.0, max_response_bytes=4 * 1024 * 1024,
                 circuit_threshold=5, circuit_half_open_after=30.0,
                 in_flight_ttl=120.0, retention_seconds=None,
                 reconcile_attempts=3, reconcile_interval=5.0, auto_reconcile=True):
        self.keyring = keyring or {}
        self.dbs = dbs
        self.issuer = issuer
        self.timeouts = {"read": 10.0, "write": 30.0, "checkout": 60.0, **(timeouts or {})}
        self.connect_timeout = connect_timeout
        self.max_response_bytes = max_response_bytes
        self.circuit_threshold = circuit_threshold
        self.circuit_half_open_after = circuit_half_open_after
        # A ledger row in 'reserved'/'executing' younger than this blocks
        # duplicate dispatch; older rows are abandoned (crash) and may be taken
        # over by an explicit same-payload retry.
        self.in_flight_ttl = in_flight_ttl
        self.retention_seconds = retention_seconds
        self.reconcile_attempts = reconcile_attempts
        self.reconcile_interval = reconcile_interval
        self.auto_reconcile = auto_reconcile
        self._circuits: dict[str, dict] = {}

    def circuit_open(self, installation_id: str) -> bool:
        state = self._circuits.get(installation_id)
        if not state or state["opened_at"] is None:
            return False
        return time.monotonic() - state["opened_at"] < self.circuit_half_open_after

    def _record_success(self, installation_id: str):
        self._circuits[installation_id] = {"failures": 0, "opened_at": None}

    def _record_failure(self, installation_id: str):
        state = self._circuits.setdefault(installation_id, {"failures": 0, "opened_at": None})
        state["failures"] += 1
        if state["failures"] >= self.circuit_threshold:
            state["opened_at"] = time.monotonic()

    def _signing_key(self, installation):
        entry = self.keyring.get(installation.environment) or self.keyring.get("default")
        if not entry:
            raise RuntimeError("No gateway execution signing key for this environment")
        return entry

    def _ip_allowed(self, ip: str, installation) -> bool:
        address = ipaddress.ip_address(ip)
        endpoint = urlsplit(installation.endpoint)
        if installation.environment == "dev" and (endpoint.hostname or "") in DEV_LOOPBACK_HOSTS:
            return address.is_loopback
        return address.is_global

    async def _resolve_checked(self, host: str, port: int, installation) -> list[str]:
        rows = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        # Preserve resolver results deterministically and prefer IPv4. ECS
        # tasks that are public-IPv4-only cannot dial Cloudflare's AAAA record.
        ips = []
        for row in rows:
            ip = row[4][0]
            if ip not in ips:
                ips.append(ip)
        ips.sort(key=lambda ip: (ipaddress.ip_address(ip).version != 4, ip))
        if not ips or any(not self._ip_allowed(ip, installation) for ip in ips):
            raise SsrFBlocked(host)
        return ips

    async def _send(self, installation, method: str, raw_path: str, raw_query: str,
                    body: bytes, token: str, deadline: float):
        return await self._send_authenticated(installation, method, raw_path, raw_query,
                                              body, {"Authorization": "Bearer " + token}, deadline)

    async def _send_authenticated(self, installation, method: str, raw_path: str, raw_query: str,
                                  body: bytes, authentication: dict, deadline: float):
        """Reuse bounded, DNS-pinned merchant transport for scoped operator probes."""
        try:
            endpoint = validate_endpoint(installation.endpoint, installation.environment)
        except ValueError as exc:
            raise SsrFBlocked(str(exc)) from exc
        scheme = endpoint.scheme
        host = endpoint.hostname
        port = endpoint.port or (443 if scheme == "https" else 80)
        target = raw_path + ("?" + raw_query if raw_query else "")
        default_port = 443 if scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        headers = {
            "Host": host_header,
            "Accept": "application/json",
            # Merchant domains commonly sit behind bot mitigation. Identify
            # the signed server-to-server caller instead of inheriting the
            # generic httpx/Python signature that some CDNs reject.
            "User-Agent": "Auteric-Gateway/1.0 (+https://auteric.com)",
        }
        headers.update(authentication)
        if body:
            headers["Content-Type"] = "application/json"
        timeout = httpx.Timeout(deadline, connect=self.connect_timeout)
        redirects = 0
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            while True:
                ips = await self._resolve_checked(host, port, installation)
                response = None
                last_error = None
                # A public hostname can legitimately return IPv6 and IPv4.
                # Fargate tasks without IPv6 must fall back to an allowed IPv4
                # address instead of treating the merchant as unreachable.
                for ip in ips:
                    literal = f"[{ip}]" if ":" in ip else ip
                    url = f"{scheme}://{literal}:{port}{target}"
                    # httpx/httpcore expects a hostname string here. Passing
                    # bytes breaks TLS handshakes for direct-IP connections.
                    extensions = {"sni_hostname": host} if scheme == "https" else None
                    try:
                        response = await client.send(client.build_request(
                            method, url, headers=headers, content=body or None, extensions=extensions
                        ), stream=True)
                        break
                    except httpx.HTTPError as exc:
                        last_error = exc
                if response is None:
                    raise last_error or httpx.ConnectError("No resolved merchant address was reachable")
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        target_url = urlsplit(location)
                        relative = not target_url.hostname and location.startswith("/")
                        if (
                            redirects >= MAX_REDIRECTS
                            or not (relative or (target_url.scheme == scheme and target_url.hostname == host))
                        ):
                            raise SsrFBlocked(location)
                        redirects += 1
                        target = location if relative else (
                            target_url.path + ("?" + target_url.query if target_url.query else "")
                        )
                        continue
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_response_bytes:
                            raise ResponseTooLarge()
                    return response.status_code, bytes(data)
                finally:
                    await response.aclose()

    def _failure(self, action, jti, digest_value, code, message, *, retryable=False,
                 outcome="failed", status_code=None, details=None):
        return TransportResult(
            outcome=outcome,
            action_id=action.action_id,
            jti=jti,
            error={
                "code": code,
                "message": message,
                "retryable": retryable,
                "action_id": action.action_id,
                "details": details or {},
            },
            status_code=status_code,
            request_hash=digest_value,
        )

    def _record_action(self, installation, action, digest_value):
        """Reserve the durable action BEFORE dispatch; classify any duplicate.

        Returns 'fresh' (reserved now), 'retry' (explicit MEP/1 §2.3 retry:
        same action_id + same payload on a settled or abandoned row),
        'conflict' (same action_id with a different payload), or 'in_flight'
        (a concurrent dispatch holds a live reservation → exactly one
        adapter/merchant effect per action_id).
        """
        if self.dbs is None:
            return "fresh"
        from ..installations import get_execution_action, insert_execution_action

        retention = (
            time.time() + self.retention_seconds if self.retention_seconds is not None else None
        )
        try:
            insert_execution_action(
                self.dbs,
                store_id=installation.store_id,
                installation_id=installation.id,
                principal=action.principal,
                operation=action.operation,
                action_id=action.action_id,
                request_hash=digest_value,
                correlation_id=action.correlation_id,
                retention_until=retention,
            )
            return "fresh"
        except self.dbs.integrity_errors:
            existing = get_execution_action(self.dbs, installation.id, action.action_id)
            if (existing["request_hash"] != digest_value or existing["principal"] != action.principal
                    or existing["operation"] != action.operation or existing["store_id"] != installation.store_id):
                return "conflict"
            if (
                existing["outcome"] in {"reserved", "executing"}
                and time.time() - existing["created_at"] < self.in_flight_ttl
            ):
                return "in_flight"
            if existing["outcome"] in {"reserved", "executing", "uncertain"}:
                # Age is not proof that the merchant did not commit. Retain
                # uncertainty instead of redispatching an abandoned write.
                from ..installations import mark_execution_action
                mark_execution_action(self.dbs, installation.id, action.action_id, "uncertain")
                return "uncertain"
            if existing["outcome"] in {"completed", "reconciled"}:
                return "receipt"
            with self.dbs.db() as db:
                claimed = db.execute("UPDATE execution_actions SET outcome='reserved',created_at=?,completed_at=NULL "
                    "WHERE installation_id=? AND action_id=? AND outcome='failed'",
                    (time.time(), installation.id, action.action_id))
                return "retry" if claimed.rowcount == 1 else "in_flight"

    def _complete_action(self, installation, action_id, outcome):
        if self.dbs is None:
            return
        from ..installations import complete_execution_action

        complete_execution_action(self.dbs, installation.id, action_id, outcome)

    def _mark_executing(self, installation, action_id):
        if self.dbs is None:
            return
        from ..installations import mark_execution_action

        mark_execution_action(self.dbs, installation.id, action_id, "executing")

    async def execute(self, installation, action: TransportAction) -> TransportResult:
        jti = action.jti or "attempt_" + secrets.token_hex(12)
        registry = contracts()
        contract = registry.OPERATIONS.get(action.operation)
        if contract is None:
            return self._failure(action, jti, None, "INVALID_INPUT", "Unknown canonical operation")
        method, raw_path, raw_query, body, remainder = _build_request(action.operation, action.input)
        try:
            registry.validate_input(action.operation, remainder)
        except Exception:
            return self._failure(action, jti, None, "INVALID_INPUT",
                                 "Input does not satisfy the canonical contract schema")
        digest_value = "sha256:" + request_hash(method, raw_path, raw_query, body)
        reservation = self._record_action(installation, action, digest_value)
        if reservation == "conflict":
            return self._failure(action, jti, digest_value, "IDEMPOTENCY_CONFLICT",
                                 "action_id was already used with a different payload")
        if reservation == "in_flight":
            return self._failure(action, jti, digest_value, "IDEMPOTENCY_CONFLICT",
                                 "action_id is already executing; reconcile by action_id "
                                 "instead of dispatching a duplicate")
        if reservation in {"receipt", "uncertain"}:
            from ..execution_receipts import get_receipt
            receipt = get_receipt(self.dbs, installation.id, action.action_id)
            if receipt:
                return TransportResult(outcome="completed", action_id=action.action_id, jti=jti,
                                       result=json.loads(receipt["result"]), request_hash=digest_value, status_code=200)
            return self._failure(action, jti, digest_value, "EXECUTION_UNCERTAIN",
                                 "Execution requires an authoritative receipt; no write was retried", outcome="uncertain")
        if self.circuit_open(installation.id):
            self._complete_action(installation, action.action_id, "failed")
            return self._failure(action, jti, digest_value, "UPSTREAM_ERROR",
                                 "Merchant circuit breaker is open for this installation",
                                 details={"circuit": "open"})
        try:
            kid, key = self._signing_key(installation)
            claims = execution_claims(installation, action, digest_value, jti, self.issuer)
            token = build_execution_jwt(key, kid, claims)
        except RuntimeError as exc:
            self._complete_action(installation, action.action_id, "failed")
            return self._failure(action, jti, digest_value, "UPSTREAM_ERROR", str(exc))
        deadline = self.timeouts[_operation_class(action.operation, contract["side_effect"])]
        side_effect = contract["side_effect"]
        self._mark_executing(installation, action.action_id)
        try:
            status, payload = await self._send(
                installation, method, raw_path, raw_query, body, token, deadline
            )
        except SsrFBlocked:
            self._complete_action(installation, action.action_id, "failed")
            return self._failure(action, jti, digest_value, "SSRF_BLOCKED",
                                 "Merchant target or redirect is not an allowed installation endpoint")
        except httpx.TimeoutException:
            self._record_failure(installation.id)
            if side_effect == "read":
                outcome, code, message = "failed", "UPSTREAM_ERROR", "Merchant read timed out"
            else:
                # The write may have been applied; reconcile via action_id.
                outcome, code, message = (
                    "uncertain", "EXECUTION_UNCERTAIN",
                    "Merchant did not confirm the write within the deadline; reconcile by action_id",
                )
            self._complete_action(installation, action.action_id, outcome)
            if outcome == "uncertain":
                self._schedule_reconciliation(installation, action.action_id)
            return self._failure(action, jti, digest_value, code, message, outcome=outcome)
        except httpx.HTTPError:
            self._record_failure(installation.id)
            outcome = "uncertain" if side_effect == "write" else "failed"
            self._complete_action(installation, action.action_id, outcome)
            return self._failure(action, jti, digest_value,
                                 "EXECUTION_UNCERTAIN" if outcome == "uncertain" else "UPSTREAM_ERROR",
                                 "Merchant transport did not confirm execution", outcome=outcome,
                                 retryable=side_effect == "read")
        except (ResponseTooLarge, asyncio.CancelledError):
            self._complete_action(installation, action.action_id, "uncertain" if side_effect == "write" else "failed")
            raise
        try:
            document = json.loads(payload) if payload else None
        except (ValueError, UnicodeDecodeError):
            document = None
            valid_json = False
        else:
            valid_json = True
        if valid_json and isinstance(document, dict) and isinstance(document.get("error"), dict):
            error = document["error"]
            code = error.get("code") if error.get("code") in ERROR_HTTP else "UPSTREAM_ERROR"
            outcome = "uncertain" if code == "EXECUTION_UNCERTAIN" else "failed"
            if outcome == "failed":
                self._record_success(installation.id)
            else:
                self._schedule_reconciliation(installation, action.action_id)
            self._complete_action(installation, action.action_id, outcome)
            return TransportResult(
                outcome=outcome,
                action_id=action.action_id,
                jti=jti,
                error={
                    "code": code,
                    "message": str(error.get("message") or "Merchant rejected the execution")[:500],
                    "retryable": bool(error.get("retryable")),
                    "action_id": action.action_id,
                    "details": error.get("details") if isinstance(error.get("details"), dict) else {},
                },
                status_code=status,
                request_hash=digest_value,
            )
        if not (200 <= status < 300):
            self._record_failure(installation.id)
            outcome = "uncertain" if side_effect == "write" and status >= 500 else "failed"
            self._complete_action(installation, action.action_id, outcome)
            return self._failure(action, jti, digest_value,
                                 "EXECUTION_UNCERTAIN" if outcome == "uncertain" else "UPSTREAM_ERROR",
                                 "Merchant returned a non-success status without an error envelope",
                                 status_code=status, outcome=outcome)
        try:
            registry.validate_output(action.operation, document)
        except Exception:
            self._record_failure(installation.id)
            outcome = "uncertain" if side_effect == "write" else "failed"
            self._complete_action(installation, action.action_id, outcome)
            return self._failure(action, jti, digest_value,
                                 "EXECUTION_UNCERTAIN" if outcome == "uncertain" else "UPSTREAM_ERROR",
                                 "Merchant response does not satisfy the canonical output schema",
                                 status_code=status, outcome=outcome)
        self._record_success(installation.id)
        if self.dbs is not None:
            from ..execution_receipts import save_receipt
            try:
                save_receipt(self.dbs, installation, action, digest_value, document)
            except Exception:
                self._complete_action(installation, action.action_id, "uncertain")
                return self._failure(action, jti, digest_value, "EXECUTION_UNCERTAIN",
                                     "Receipt could not be persisted; reconcile by action_id", outcome="uncertain")
        else:
            self._complete_action(installation, action.action_id, "completed")
        return TransportResult(
            outcome="completed",
            action_id=action.action_id,
            jti=jti,
            result=document,
            status_code=status,
            request_hash=digest_value,
        )

    async def inspect_health(self, installation) -> dict:
        """Authenticated probe per MEP/1; returns contract/release evidence."""
        observed_at = time.time()
        if installation.revoked_at:
            return {"reachable": False, "reason": "revoked", "contract_digest": None,
                    "release_id": installation.release_id, "observed_at": observed_at}
        if self.circuit_open(installation.id):
            return {"reachable": False, "reason": "circuit_open", "contract_digest": None,
                    "release_id": installation.release_id, "observed_at": observed_at}
        body = b""
        digest_value = "sha256:" + request_hash("GET", "/api/auteric/v1/health", "", body)
        probe = TransportAction(
            operation="health",
            input={},
            principal="gateway_health_probe",
            action_id="action_" + secrets.token_hex(12),
            contract_version=registry_version(),
            binding_digest="sha256:" + "0" * 64,
        )
        try:
            kid, key = self._signing_key(installation)
            claims = execution_claims(installation, probe, digest_value,
                                      "attempt_" + secrets.token_hex(12), self.issuer)
            token = build_execution_jwt(key, kid, claims)
            status, payload = await self._send(
                installation, "GET", "/api/auteric/v1/health", "", body, token, self.timeouts["read"]
            )
        except Exception as exc:
            if not isinstance(exc, (SsrFBlocked, RuntimeError)):
                self._record_failure(installation.id)
            return {"reachable": False, "reason": "unreachable", "contract_digest": None,
                    "release_id": installation.release_id, "observed_at": observed_at}
        try:
            document = json.loads(payload) if payload else {}
        except ValueError:
            document = {}
        reachable = 200 <= status < 300
        if reachable:
            self._record_success(installation.id)
        else:
            self._record_failure(installation.id)
        return {
            "reachable": reachable,
            "status_code": status,
            "contract_digest": document.get("contract_digest") if isinstance(document, dict) else None,
            "release_id": (document.get("release_id") if isinstance(document, dict) else None) or installation.release_id,
            "observed_at": observed_at,
        }

    async def _merchant_reconcile_probe(self, installation, action_id: str):
        """Read-only merchant probe for an uncertain action; NEVER re-executes.

        MEP/1 reconciliation asks the merchant's action ledger
        (GET /api/auteric/v1/actions/{action_id}) for the stored outcome of the
        original action_id. Returns 'applied' (+ optional stored result),
        'not_applied', or None when the merchant cannot confirm either way.
        """
        raw_path = "/api/auteric/v1/actions/" + quote(action_id, safe="")
        digest_value = "sha256:" + request_hash("GET", raw_path, "", b"")
        probe = TransportAction(
            operation="reconcile",
            input={},
            principal="gateway_reconcile_probe",
            action_id=action_id,
            contract_version=registry_version(),
            binding_digest="sha256:" + "0" * 64,
        )
        try:
            kid, key = self._signing_key(installation)
            claims = execution_claims(installation, probe, digest_value,
                                      "attempt_" + secrets.token_hex(12), self.issuer)
            token = build_execution_jwt(key, kid, claims)
            status, payload = await self._send(
                installation, "GET", raw_path, "", b"", token, self.timeouts["read"]
            )
        except Exception:
            return None
        if status == 404:
            # The merchant has no record yet; the write may still be in flight
            # or never arrived. Stay uncertain rather than guessing.
            return None
        if not (200 <= status < 300):
            return None
        try:
            document = json.loads(payload) if payload else None
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict) or document.get("action_id") != action_id:
            return None
        outcome = document.get("outcome")
        if outcome in {"completed", "applied"}:
            return {"state": "applied", "result": document.get("result")}
        if outcome in {"failed", "not_applied"}:
            return {"state": "not_applied"}
        return None

    def _schedule_reconciliation(self, installation, action_id: str):
        """Bounded in-process reconciliation task for an uncertain write.

        Suitable for tests and the single-node pilot. Production deployments
        should drive the same `reconcile` call from a durable worker; if this
        process dies, the ledger row simply stays 'uncertain' (never silently
        cleared) and reconciliation resumes from the durable record.
        """
        if self.dbs is None or not self.auto_reconcile:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def drive():
            for _ in range(self.reconcile_attempts):
                await asyncio.sleep(self.reconcile_interval)
                try:
                    record = await self.reconcile(installation, action_id)
                except Exception:
                    record = None
                if record and record.get("outcome") != "uncertain":
                    return

        return loop.create_task(drive())

    async def reconcile(self, installation, action_id: str) -> dict | None:
        if self.dbs is None:
            return None
        from ..installations import complete_execution_action, get_execution_action

        row = get_execution_action(self.dbs, installation.id, action_id)
        if not row:
            return None
        result = None
        if row["outcome"] == "uncertain":
            probe = await self._merchant_reconcile_probe(installation, action_id)
            if probe and probe["state"] == "applied":
                result = probe.get("result")
                try:
                    contracts().validate_output(row["operation"], result)
                except Exception:
                    return {**row, "result": None}
                from ..execution_receipts import save_receipt
                recovered = TransportAction(operation=row["operation"], input={}, principal=row["principal"],
                    action_id=action_id, contract_version=registry_version(), binding_digest="")
                save_receipt(self.dbs, installation, recovered, row["request_hash"], result, outcome="reconciled")
                row["outcome"] = "reconciled"
                row["completed_at"] = time.time()
            elif probe and probe["state"] == "not_applied":
                complete_execution_action(self.dbs, installation.id, action_id, "failed")
                row["outcome"] = "failed"
                row["completed_at"] = time.time()
        if result is None and row["outcome"] in {"completed", "reconciled"}:
            from ..execution_receipts import get_receipt
            receipt = get_receipt(self.dbs, installation.id, action_id)
            result = json.loads(receipt["result"]) if receipt else None
        return {
            "action_id": row["action_id"],
            "operation": row["operation"],
            "outcome": row["outcome"],
            "request_hash": row["request_hash"],
            "correlation_id": row["correlation_id"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "result": result,
        }


def registry_version() -> str:
    return contracts().REGISTRY_VERSION
