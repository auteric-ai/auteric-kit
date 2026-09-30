"""Installation model (P1-04): registration, trust binding, revocation, backfill.

An installation binds one store+environment to an execution transport
("native_http" or "outbound_worker") with its MEP/1 trust bundle. Registration
is an authenticated merchant-operator action; environment binding is enforced
so staging credentials can never register or overwrite a production endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .storage import digest, encode, uid
from .transports.native_http import validate_endpoint

TRANSPORTS = {"native_http", "outbound_worker"}
ENVIRONMENTS = {"production", "staging", "sandbox", "dev"}
SIDECAR_REGISTRATION_SCHEMA = "auteric-sidecar-registration/v1"
# Checkout creation is deliberately allowed: it only returns the merchant's
# payment handoff.  Autonomous payment completion is never part of this MVP.
SIDECAR_FORBIDDEN_OPERATIONS = {"complete_checkout"}
SIDECAR_RUNTIME_EVIDENCE_KIND = "sidecar_runtime"


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sidecar_runtime_evidence(raw, *, operation: str, environment: str,
                             mapping_fingerprint: str, contract_fingerprint: str,
                             now: float | None = None) -> dict | None:
    """Validate the proof that permits one generated profile to run.

    Generic project tests are useful preparation evidence, but must never turn
    on a deployable sidecar profile.  This record is deliberately bound to the
    profile mapping, runtime contract, and environment so a registration or
    contract change makes the old proof unusable.
    """
    if not raw:
        return None
    try:
        evidence = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
    required = {
        "kind": SIDECAR_RUNTIME_EVIDENCE_KIND,
        "status": "pass",
        "operation": operation,
        "environment": environment,
        "mapping_fingerprint": mapping_fingerprint,
        "contract_fingerprint": contract_fingerprint,
    }
    if any(evidence.get(key) != value for key, value in required.items()):
        return None
    if not isinstance(evidence.get("evidence_id"), str) or not evidence["evidence_id"]:
        return None
    suites = evidence.get("test_suites")
    if not isinstance(suites, list) or not suites or not all(isinstance(item, str) and item for item in suites):
        return None
    try:
        passed_at, expires_at = float(evidence["passed_at"]), float(evidence["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if passed_at <= 0 or expires_at <= passed_at or expires_at <= current:
        return None
    return {
        "evidence_id": evidence["evidence_id"], "operation": operation,
        "environment": environment, "mapping_fingerprint": mapping_fingerprint,
        "contract_fingerprint": contract_fingerprint, "passed_at": passed_at,
        "expires_at": expires_at, "test_suites": suites,
        "payment_handler_verified": bool(evidence.get("payment_handler_verified", False)),
    }


class NativeSidecarRegistration(BaseModel):
    """Public, generated input for a deployable sidecar bundle.

    The profile mappings are intentionally supplied by the installer after it
    has inspected the merchant API.  The control plane pins and validates the
    input; it never guesses a route or accepts credentials in this document.
    """

    model_config = ConfigDict(extra="forbid")
    schema: str = SIDECAR_REGISTRATION_SCHEMA
    integration_version: str = Field(min_length=1, max_length=200)
    profiles: dict[str, dict] = Field(min_length=1, max_length=30)
    allowed_operations: list[str] = Field(min_length=1, max_length=30)
    policy_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    storage_mode: str = "ephemeral"
    storage_backend: str = "postgres"
    agent_ingress: bool = False

    @model_validator(mode="after")
    def validate_sidecar_input(self):
        if self.schema != SIDECAR_REGISTRATION_SCHEMA:
            raise ValueError("unsupported sidecar registration schema")
        if self.storage_mode not in {"ephemeral", "durable"}:
            raise ValueError("sidecar storage must be ephemeral preview or durable PostgreSQL")
        if self.storage_backend not in {'postgres','sqlite'}:
            raise ValueError('Unsupported storage backend')
        if set(self.profiles) != set(self.allowed_operations):
            raise ValueError("sidecar profiles and policy operations must match exactly")
        if len(set(self.allowed_operations)) != len(self.allowed_operations):
            raise ValueError("sidecar policy operations must be unique")
        if SIDECAR_FORBIDDEN_OPERATIONS & set(self.profiles):
            raise ValueError("complete_checkout/payment operations are not supported by this sidecar MVP")
        for operation, profile in self.profiles.items():
            if not isinstance(profile, dict) or profile.get("operation") != operation:
                raise ValueError("each sidecar profile must name its canonical operation")
            if not isinstance(profile.get("mapping_fingerprint"), str) or not profile["mapping_fingerprint"].startswith("sha256:"):
                raise ValueError("each sidecar profile requires a sha256 mapping fingerprint")
            target = profile.get("target")
            if not isinstance(target, dict) or set(target) - {
                "base_url", "method", "path", "timeout_seconds", "retries", "max_response_bytes",
                "auth_scheme", "credential_ref", "credential_header",
            }:
                raise ValueError("sidecar targets may contain only public bridge routing fields")
            base_url = target.get("base_url", "")
            if base_url not in {"http://127.0.0.1:3101", "http://localhost:3101"}:
                raise ValueError("sidecar target must be the task-local bridge at port 3101")
            if not isinstance(target.get("path"), str) or not target["path"].startswith("/") or "//" in target["path"]:
                raise ValueError("sidecar target path must be an absolute normalized path")
            if profile.get("enabled") is not False:
                raise ValueError("generated sidecar profiles must remain disabled until runtime verification")
            # The sole permitted credential is the task-local bridge token.
            # It is a reference, never a token value, and pins the sidecar to
            # the bridge that shares its ECS task rather than exposing it.
            if (target.get("auth_scheme") != "bearer"
                    or target.get("credential_ref") != "env:AUTERIC_BRIDGE_TOKEN"
                    or target.get("credential_header") != "authorization"):
                raise ValueError("sidecar target must use the AUTERIC_BRIDGE_TOKEN bearer reference")
        return self


class NativeRuntimeRegistration(BaseModel):
    """Merchant-pinned, non-secret runtime parameters.

    The Gateway's signing key is deliberately *not* accepted here.  It is
    derived by the control plane at configuration retrieval time so a
    merchant cannot replace the authority that signs execution credentials.
    """

    model_config = ConfigDict(extra="forbid")
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trusted_proxy_prefix: str | None = Field(default=None, max_length=200)
    max_body_bytes: int = Field(default=1024 * 1024, ge=1, le=4 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_proxy_prefix(self):
        prefix = self.trusted_proxy_prefix
        if prefix is not None and (
            not prefix.startswith("/")
            or prefix.endswith("/")
            or "//" in prefix
            or "/../" in prefix
            or prefix in {"/.", "/.."}
        ):
            raise ValueError("trusted_proxy_prefix must be an absolute normalized non-root prefix")
        return self


@dataclass
class Installation:
    id: str
    store_id: str
    environment: str
    transport: str
    endpoint: str
    trust_binding: dict = field(default_factory=dict)
    protocol_version: str = "1"
    sdk_version: str | None = None
    release_id: str | None = None
    revoked_at: float | None = None
    created_at: float | None = None
    updated_at: float | None = None

    @classmethod
    def from_row(cls, row) -> "Installation":
        return cls(
            id=row["id"],
            store_id=row["store_id"],
            environment=row["environment"],
            transport=row["transport"],
            endpoint=row["endpoint"],
            trust_binding=json.loads(row["trust_binding"] or "{}"),
            protocol_version=row["protocol_version"],
            sdk_version=row["sdk_version"],
            release_id=row["release_id"],
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def view(self) -> dict:
        return {
            "id": self.id,
            "store_id": self.store_id,
            "environment": self.environment,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "trust_binding": self.trust_binding,
            "protocol_version": self.protocol_version,
            "sdk_version": self.sdk_version,
            "release_id": self.release_id,
            "revoked_at": self.revoked_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def get_installation(dbs, store_id, environment):
    with dbs.db() as db:
        row = db.execute(
            "SELECT * FROM installations WHERE store_id=? AND environment=?", (store_id, environment)
        ).fetchone()
    return dict(row) if row else None


def get_installation_by_id(dbs, installation_id):
    with dbs.db() as db:
        row = db.execute("SELECT * FROM installations WHERE id=?", (installation_id,)).fetchone()
    return dict(row) if row else None


def upsert_installation(dbs, *, store_id, environment, transport, endpoint, trust_binding,
                        protocol_version="1", sdk_version=None, release_id=None):
    """Register or update the unique (store_id, environment) installation.

    Re-registering a revoked installation reactivates it; every transition is
    audited by the calling route.
    """
    if transport not in TRANSPORTS:
        raise ValueError("Unsupported installation transport")
    if environment not in ENVIRONMENTS:
        raise ValueError("Unsupported installation environment")
    now = time.time()
    with dbs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id FROM installations WHERE store_id=? AND environment=?", (store_id, environment)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE installations SET transport=?,endpoint=?,trust_binding=?,protocol_version=?,"
                "sdk_version=?,release_id=?,revoked_at=NULL,updated_at=? WHERE id=?",
                (transport, endpoint, encode(trust_binding), protocol_version,
                 sdk_version, release_id, now, existing["id"]),
            )
            installation_id = existing["id"]
            created = False
        else:
            installation_id = "install_" + uid()
            db.execute(
                "INSERT INTO installations(id,store_id,environment,transport,endpoint,trust_binding,"
                "protocol_version,sdk_version,release_id,revoked_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?)",
                (installation_id, store_id, environment, transport, endpoint, encode(trust_binding),
                 protocol_version, sdk_version, release_id, now, now),
            )
            created = True
        row = db.execute("SELECT * FROM installations WHERE id=?", (installation_id,)).fetchone()
    return dict(row), created


def revoke_installation(dbs, installation_id):
    with dbs.db() as db:
        changed = db.execute(
            "UPDATE installations SET revoked_at=?,updated_at=? WHERE id=? AND revoked_at IS NULL",
            (time.time(), time.time(), installation_id),
        ).rowcount
    return bool(changed)


def record_installation_operations(dbs, installation_id, operations):
    with dbs.db() as db:
        for item in operations:
            existing = db.execute(
                "SELECT contract_digest,binding_digest,test_evidence,deployment_evidence "
                "FROM installation_operations WHERE installation_id=? AND operation=?",
                (installation_id, item["operation"]),
            ).fetchone()
            test_evidence = item.get("test_evidence")
            deployment_evidence = item.get("deployment_evidence")
            if (
                existing
                and existing["contract_digest"] == item["contract_digest"]
                and existing["binding_digest"] == item["binding_digest"]
            ):
                try:
                    previous_test = json.loads(existing["test_evidence"] or "null")
                except (TypeError, ValueError):
                    previous_test = None
                try:
                    incoming_test = dict(test_evidence) if test_evidence is not None else None
                except (TypeError, ValueError):
                    incoming_test = None
                # A rerun may submit build/prepare evidence again. Never let
                # that weaker evidence erase a live sidecar proof for the same
                # installed contract. A changed mapping remains invalid because
                # sidecar_config checks its mapping fingerprint separately.
                if (
                    isinstance(previous_test, dict)
                    and previous_test.get("kind") == SIDECAR_RUNTIME_EVIDENCE_KIND
                    and not (
                        isinstance(incoming_test, dict)
                        and incoming_test.get("kind") == SIDECAR_RUNTIME_EVIDENCE_KIND
                    )
                ):
                    test_evidence = previous_test
                if deployment_evidence is None and existing["deployment_evidence"]:
                    try:
                        deployment_evidence = json.loads(existing["deployment_evidence"])
                    except (TypeError, ValueError):
                        deployment_evidence = None
            db.execute(
                "INSERT INTO installation_operations(installation_id,operation,contract_digest,"
                "binding_digest,test_evidence,deployment_evidence) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(installation_id,operation) DO UPDATE SET "
                "contract_digest=excluded.contract_digest,binding_digest=excluded.binding_digest,"
                "test_evidence=excluded.test_evidence,deployment_evidence=excluded.deployment_evidence",
                (
                    installation_id,
                    item["operation"],
                    item["contract_digest"],
                    item["binding_digest"],
                    encode(test_evidence) if test_evidence is not None else None,
                    encode(deployment_evidence) if deployment_evidence is not None else None,
                ),
            )


def set_capability_policy(dbs, store_id, operation, enabled, actor):
    now = time.time()
    with dbs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT revision FROM capability_policies WHERE store_id=? AND operation=?",
            (store_id, operation),
        ).fetchone()
        revision = (row["revision"] + 1) if row else 1
        db.execute(
            "INSERT INTO capability_policies(store_id,operation,enabled,revision,actor,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(store_id,operation) DO UPDATE SET "
            "enabled=excluded.enabled,revision=excluded.revision,actor=excluded.actor,"
            "updated_at=excluded.updated_at",
            (store_id, operation, int(enabled), revision, actor, now),
        )
    return revision


DEFAULT_ACTION_RETENTION_SECONDS = 30 * 86400


def insert_execution_action(dbs, *, store_id, installation_id, principal, operation,
                            action_id, request_hash, correlation_id=None, outcome="reserved",
                            retention_until=None):
    """Durable action record written BEFORE dispatch; UNIQUE(installation_id, action_id).

    Lifecycle (plan §12): reserved → executing → completed / failed, with
    uncertain → reconciled for writes whose outcome was never confirmed.
    """
    created = time.time()
    with dbs.db() as db:
        db.execute(
            "INSERT INTO execution_actions(store_id,installation_id,principal,operation,action_id,"
            "request_hash,outcome,correlation_id,created_at,completed_at,retention_until) "
            "VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",
            (store_id, installation_id, principal, operation, action_id,
             request_hash, outcome, correlation_id, created,
             retention_until if retention_until is not None else created + DEFAULT_ACTION_RETENTION_SECONDS),
        )


def mark_execution_action(dbs, installation_id, action_id, outcome):
    """Transition without touching completed_at (e.g. reserved → executing)."""
    with dbs.db() as db:
        db.execute(
            "UPDATE execution_actions SET outcome=? WHERE installation_id=? AND action_id=?",
            (outcome, installation_id, action_id),
        )


def cleanup_execution_actions(dbs, now=None):
    """Delete settled ledger rows past their retention deadline.

    Unresolved rows ('reserved', 'executing', 'uncertain') are NEVER deleted:
    an uncertain write must stay reconcilable by action_id for as long as its
    outcome is unknown (plan §12 retention rule).
    """
    now = time.time() if now is None else now
    with dbs.db() as db:
        changed = db.execute(
            "DELETE FROM execution_actions WHERE retention_until IS NOT NULL AND retention_until<? "
            "AND outcome IN ('completed','failed','reconciled')",
            (now,),
        ).rowcount
    return changed


def complete_execution_action(dbs, installation_id, action_id, outcome):
    with dbs.db() as db:
        db.execute(
            "UPDATE execution_actions SET outcome=?,completed_at=? WHERE installation_id=? AND action_id=? "
            "AND outcome NOT IN ('completed','reconciled')",
            (outcome, time.time(), installation_id, action_id),
        )


def get_execution_action(dbs, installation_id, action_id):
    with dbs.db() as db:
        row = db.execute(
            "SELECT * FROM execution_actions WHERE installation_id=? AND action_id=?",
            (installation_id, action_id),
        ).fetchone()
    return dict(row) if row else None


def backfill_worker_installations(dbs):
    """Map existing worker-based connections to outbound_worker installations.

    A worker connection is evidenced by a connector heartbeat on the store.
    Idempotent: stores that already have an installation row are untouched.
    """
    created = 0
    with dbs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id,environment,connector_release FROM stores WHERE heartbeat IS NOT NULL"
        ).fetchall()
        for row in rows:
            existing = db.execute(
                "SELECT 1 FROM installations WHERE store_id=? AND environment=?",
                (row["id"], row["environment"]),
            ).fetchone()
            if existing:
                continue
            now = time.time()
            db.execute(
                "INSERT INTO installations(id,store_id,environment,transport,endpoint,trust_binding,"
                "protocol_version,sdk_version,release_id,revoked_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?)",
                (
                    "install_" + uid(),
                    row["id"],
                    row["environment"],
                    "outbound_worker",
                    "outbound+worker://store/" + row["id"],
                    "{}",
                    "1",
                    None,
                    row["connector_release"],
                    now,
                    now,
                ),
            )
            created += 1
    return created


class InstallationRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str
    transport: str = "native_http"
    endpoint: str = Field(min_length=1, max_length=500)
    trust_binding: dict = Field(default_factory=dict)
    protocol_version: str = Field(default="1", max_length=10)
    sdk_version: str | None = Field(default=None, max_length=100)
    release_id: str | None = Field(default=None, max_length=200)
    operations: list[dict] = Field(default_factory=list, max_length=50)
    # This is optional only for compatibility with existing worker and legacy
    # native rows. A Phase 1 native runtime must supply it before it can fetch
    # its pinned configuration.
    native_runtime: NativeRuntimeRegistration | None = None
    # Optional because legacy native installations remain supported.  When
    # present it is public generated deployment input, not a runtime secret.
    sidecar: NativeSidecarRegistration | None = None


def native_runtime_settings(row) -> dict:
    """Return the validated, merchant-pinned native runtime settings.

    The configuration is stored in the existing JSON trust_binding column for
    an additive migration, but only this narrow sub-document is used for
    execution.  No key material is ever taken from it.
    """
    try:
        raw = json.loads(row["trust_binding"] or "{}")
        config = NativeRuntimeRegistration.model_validate(raw.get("native_runtime"))
    except Exception as exc:
        raise ValueError("Native installation has no valid runtime configuration") from exc
    return config.model_dump(exclude_none=True)


def native_runtime_binding_digest(row) -> str:
    return native_runtime_settings(row)["binding_digest"]


def attach_installations(app, dbs):
    state = app.state
    prefix = "/api/commerce/stores/{store_id}/installations"

    def installation_view(row):
        return Installation.from_row(row).view()

    @app.get(prefix)
    def list_installations(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            rows = db.execute(
                "SELECT * FROM installations WHERE store_id=? ORDER BY created_at", (store_id,)
            ).fetchall()
        return {"installations": [installation_view(row) for row in rows]}

    @app.post(prefix)
    def register(store_id: str, body: InstallationRegistration, response: Response,
                 actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        if body.environment not in ENVIRONMENTS:
            raise HTTPException(422, "Unsupported installation environment")
        if body.transport not in TRANSPORTS:
            raise HTTPException(422, "Unsupported installation transport")
        # Environment binding: a token scoped to a staging/sandbox store can
        # never register or overwrite a production endpoint.
        if body.environment != store["environment"]:
            raise HTTPException(
                422,
                "Installation environment must match the store environment; "
                "staging credentials cannot register a production endpoint",
            )
        if body.transport == "native_http":
            try:
                validate_endpoint(body.endpoint, body.environment)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        # Keep the historical opaque trust_binding for migrations, while
        # reserving native_runtime for the strict, non-secret runtime shape.
        trust_binding = dict(body.trust_binding)
        if body.native_runtime is not None:
            trust_binding["native_runtime"] = body.native_runtime.model_dump(exclude_none=True)
        if body.sidecar is not None:
            registered = body.sidecar.model_dump()
            # Pin the full public mapping; this digest is returned to the
            # installer and lets deployment evidence identify one exact bundle.
            registered["bundle_digest"] = _sha256(registered)
            trust_binding["sidecar"] = registered
        row, created = upsert_installation(
            dbs,
            store_id=store_id,
            environment=body.environment,
            transport=body.transport,
            endpoint=body.endpoint,
            trust_binding=trust_binding,
            protocol_version=body.protocol_version,
            sdk_version=body.sdk_version,
            release_id=body.release_id,
        )
        if body.operations:
            record_installation_operations(dbs, row["id"], body.operations)
        dbs.event(
            actor["org"],
            store_id,
            actor["id"],
            "installation.registered" if created else "installation.updated",
            {"installation_id": row["id"], "environment": body.environment,
             "transport": body.transport, "endpoint_digest": digest(body.endpoint)},
        )
        response.status_code = 201 if created else 200
        return installation_view(row)

    def owned_installation(store_id, installation_id, actor):
        state.owned(store_id, actor)
        row = get_installation_by_id(dbs, installation_id)
        if not row or row["store_id"] != store_id:
            raise HTTPException(404, "Installation not found for this store")
        return row

    @app.get(prefix + "/{installation_id}/native-runtime-config")
    def native_runtime_config(store_id: str, installation_id: str,
                              actor=Depends(state.user_dependency)):
        """Return the public-only pinned config consumed by a native SDK.

        This is deliberately an authenticated merchant-operator endpoint, not
        a discovery endpoint. It returns an Ed25519 *public* execution key;
        the private signing seed remains exclusively inside the Gateway.
        """
        row = owned_installation(store_id, installation_id, actor)
        if row["transport"] != "native_http":
            raise HTTPException(409, "Installation does not use native HTTP transport")
        if row["revoked_at"]:
            raise HTTPException(409, "Installation is revoked")
        try:
            runtime = native_runtime_settings(row)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        with dbs.db() as db:
            operations = [dict(item) for item in db.execute(
                "SELECT operation,binding_digest FROM installation_operations "
                "WHERE installation_id=? ORDER BY operation", (installation_id,)
            ).fetchall()]
        if not operations:
            raise HTTPException(409, "Native installation has no registered operations")
        if any(item["binding_digest"] != runtime["binding_digest"] for item in operations):
            raise HTTPException(409, "Registered operation binding digests do not match native runtime configuration")
        transport = getattr(state, "native_transport", None)
        if transport is None:
            raise HTTPException(503, "Native HTTP execution signing is not configured")
        try:
            kid, private_key = transport._signing_key(Installation.from_row(row))
        except Exception:
            raise HTTPException(503, "Native HTTP execution signing is not configured") from None
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return {
            "config_version": "auteric-native-runtime/v1",
            "installation": {
                "installationId": row["id"],
                "storeId": row["store_id"],
                "environment": row["environment"],
                "enabled": True,
                "operations": [item["operation"] for item in operations],
                "bindingDigest": runtime["binding_digest"],
                "trustedProxyPrefix": runtime.get("trusted_proxy_prefix"),
                "maxBodyBytes": runtime["max_body_bytes"],
            },
            "trust": {
                "issuers": [transport.issuer],
                "keys": {kid: base64.urlsafe_b64encode(public_key).decode().rstrip("=")},
            },
            "endpoint": row["endpoint"],
            "release_id": row["release_id"],
        }

    @app.get(prefix + "/{installation_id}/sidecar-config")
    def sidecar_config(store_id: str, installation_id: str,
                       actor=Depends(state.user_dependency)):
        """Return one immutable public ``auteric-sidecar/v1`` bundle.

        This endpoint is owner-scoped and intentionally returns neither
        control credentials nor merchant secrets.  It is available only for
        explicitly generated bridge profiles; native runtime metadata alone
        is not sufficient to manufacture an integration.
        """
        row = owned_installation(store_id, installation_id, actor)
        if row["transport"] != "native_http":
            raise HTTPException(409, "Installation does not use native HTTP transport")
        if row["revoked_at"]:
            raise HTTPException(409, "Installation is revoked")
        try:
            runtime = native_runtime_settings(row)
            binding = json.loads(row["trust_binding"] or "{}")
            raw_sidecar = dict(binding.get("sidecar") or {})
            recorded_digest = raw_sidecar.pop("bundle_digest", None)
            sidecar = NativeSidecarRegistration.model_validate(raw_sidecar)
        except Exception:
            raise HTTPException(409, "Native installation has no valid generated sidecar configuration") from None
        if recorded_digest != _sha256(sidecar.model_dump()):
            raise HTTPException(409, "Generated sidecar configuration digest does not match")
        with dbs.db() as db:
            operations = {item["operation"]: dict(item) for item in db.execute(
                "SELECT operation,contract_digest,binding_digest,test_evidence,deployment_evidence "
                "FROM installation_operations WHERE installation_id=?",
                (installation_id,)
            ).fetchall()}
        if set(sidecar.profiles) - set(operations):
            raise HTTPException(409, "Generated sidecar profiles are not registered installation operations")
        if any(item["binding_digest"] != runtime["binding_digest"] for item in operations.values()):
            raise HTTPException(409, "Registered operation binding digests do not match native runtime configuration")
        transport = getattr(state, "native_transport", None)
        if transport is None:
            raise HTTPException(503, "Native HTTP execution signing is not configured")
        try:
            kid, private_key = transport._signing_key(Installation.from_row(row))
        except Exception:
            raise HTTPException(503, "Native HTTP execution signing is not configured") from None
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        registry = __import__("services.commerce.transports.native_http", fromlist=["contracts"]).contracts()
        # A sidecar profile is enabled only when the merchant has enabled the
        # capability *and* a current runtime test proved this exact bridge
        # mapping.  A generic unit/prepare result is intentionally insufficient.
        from .capabilities import operation_states
        store = state.store_row(store_id)
        runtime_evidence = {}
        enabled_operations = []
        for operation, profile in sidecar.profiles.items():
            evidence = sidecar_runtime_evidence(
                operations[operation]["test_evidence"], operation=operation,
                environment=row["environment"],
                mapping_fingerprint=profile["mapping_fingerprint"],
                contract_fingerprint=operations[operation]["binding_digest"],
            )
            runtime_evidence[operation] = evidence
            states = operation_states(dbs, store, operation)
            if evidence is not None and states["enabled"]:
                enabled_operations.append(operation)
        issued_at = time.time()
        # Policy is a short-lived, fail-closed snapshot.  On a policy toggle,
        # mapping re-registration, or installation revocation, newly fetched
        # configuration cannot preserve an old enabled profile.
        policy = {
            "allowed_operations": sorted(enabled_operations), "issued_at": issued_at,
            "expires_at": issued_at + sidecar.policy_ttl_seconds,
        }
        manifest = {
            operation: {
                # The MEP runtime contract is determined by the deployed
                # sidecar registry; this binding pins this config to the
                # installation's checked bridge mapping.
                "method": registry.OPERATIONS[operation]["method"],
                "path": registry.OPERATIONS[operation]["path"],
                "contract_version": registry.OPERATIONS[operation]["contract_version"],
                "binding_digest": operations[operation]["binding_digest"],
            }
            for operation in sorted(sidecar.profiles)
        }
        profiles = {}
        for operation, profile in sidecar.profiles.items():
            evidence = runtime_evidence[operation]
            profiles[operation] = {
                **profile,
                "integration_version": sidecar.integration_version,
                "contract_fingerprint": operations[operation]["binding_digest"],
                "enabled": operation in enabled_operations,
                "verification": evidence if operation in enabled_operations else None,
            }
        return {
            "schema": "auteric-sidecar/v1",
            "installation": {
                "installation_id": row["id"], "store_id": row["store_id"],
                "environment": row["environment"], "enabled": True,
                "root_path": runtime.get("trusted_proxy_prefix"), "manifest": manifest,
            },
            "trust": {
                "issuer_allowlist": [transport.issuer],
                "keys": {kid: base64.urlsafe_b64encode(public_key).decode().rstrip("=")},
            },
            "integration": {
                "store_id": row["store_id"], "installation_id": row["id"],
                "environment": row["environment"], "integration_version": sidecar.integration_version,
                # These are verified by the sidecar package at startup.  The
                # installer must pin them from the installed package release.
                "merchant_protocol": "1", "registry_version": registry.REGISTRY_VERSION,
                "registry_digest": registry.REGISTRY_DIGEST, "profiles": profiles,
            },
            "operational": {
                "storage_mode": sidecar.storage_mode,
                "execution_store": "postgres" if sidecar.storage_mode == "durable" and sidecar.storage_backend=='postgres' else "sqlite:/data/auteric-executions.sqlite",
                "audit_store": "postgres" if sidecar.storage_mode == "durable" and sidecar.storage_backend=='postgres' else "sqlite:/data/auteric-audit.sqlite",
                **({"database_secret_ref": "env:AUTERIC_SIDECAR_DATABASE_URL"} if sidecar.storage_mode == "durable" and sidecar.storage_backend=='postgres' else {}),
                **({'agent_ingress': {'gateway_url':state.public_url,'credential_ref':'env:AUTERIC_SIDECAR_GATEWAY_TOKEN',
                    **({'allow_dev_http':True} if row['environment']=='dev' else {})},
                    'control_token_ref':'env:AUTERIC_SIDECAR_GATEWAY_TOKEN'} if sidecar.agent_ingress else {}),
                "policy": {"fingerprint": _sha256(policy), **policy},
            },
        }

    state.sidecar_config = sidecar_config

    @app.get(prefix + "/{installation_id}")
    def get_one(store_id: str, installation_id: str, actor=Depends(state.user_dependency)):
        return installation_view(owned_installation(store_id, installation_id, actor))

    @app.post(prefix + "/{installation_id}/verify")
    async def verify(store_id: str, installation_id: str, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        row = owned_installation(store_id, installation_id, actor)
        if row["revoked_at"]:
            raise HTTPException(409, "Installation is revoked")
        if row["transport"] == "native_http":
            transport = getattr(state, "native_transport", None)
            if transport is None:
                raise HTTPException(503, "Native HTTP execution signing is not configured")
            result = await transport.inspect_health(Installation.from_row(row))
        else:
            fresh = bool(store["heartbeat"] and store["heartbeat"] > time.time() - 40)
            result = {
                "reachable": fresh,
                "last_heartbeat": store["heartbeat"],
                "contract_digest": None,
                "release_id": store["connector_release"] or row["release_id"],
                "observed_at": time.time(),
            }
        dbs.event(
            actor["org"],
            store_id,
            actor["id"],
            "installation.verified" if result.get("reachable") else "installation.verify_failed",
            {"installation_id": installation_id, "reachable": bool(result.get("reachable"))},
        )
        return result

    @app.post(prefix + "/{installation_id}/revoke")
    async def revoke(store_id: str, installation_id: str, actor=Depends(state.user_dependency)):
        row = owned_installation(store_id, installation_id, actor)
        if not revoke_installation(dbs, installation_id) and not row["revoked_at"]:
            raise HTTPException(409, "Installation could not be revoked")
        dbs.event(actor["org"], store_id, actor["id"], "installation.revoked",
                  {"installation_id": installation_id})
        return {"revoked": True, "installation_id": installation_id,
                "scanner_evidence": await state.invalidate_scanner_evidence(state.store_row(store_id))}

    @app.get(prefix + "/{installation_id}/actions/{action_id}")
    def execution_action(store_id: str, installation_id: str, action_id: str,
                         actor=Depends(state.user_dependency)):
        owned_installation(store_id, installation_id, actor)
        row = get_execution_action(dbs, installation_id, action_id)
        if not row:
            raise HTTPException(404, "Execution action not found")
        return row
