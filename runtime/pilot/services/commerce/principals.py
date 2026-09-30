"""Principal bridge (P1-09): pairwise buyer/guest principals and resource ownership.

Identity rules (plan §11):

- The merchant never sees the raw external subject (agent credential, session id
  or buyer account claim). Every external subject is mapped to a *pairwise*
  merchant principal: ``guest_``/``buyer_``/``service_`` + 128 bits of randomness,
  scoped to exactly one installation. The same external subject in two
  installations yields two unrelated principals, so records can never be shared
  or correlated across stores/installations.
- A pairwise principal is **never** derived from a raw email address or any
  other unverified claim. Account-level identity (``buyer`` kind) is created only
  through ``link_principal`` with an explicit ``consent_reference`` recorded by
  the caller; the bridge itself performs no identity verification and accepts
  only subjects that an authenticated caller presents.
- Ownership of merchant resources (carts, checkouts, orders) is preferably
  enforced by the merchant's own model (MEP/1 §2.2 step 11). The
  ``resource_owners`` table is a fallback used only when the merchant model
  lacks an ownership representation: a record, once present, is authoritative at
  the gateway (a different principal is denied before any transport call); the
  absence of a record defers to merchant-native enforcement.
- Denial shape for foreign resources is a FIXED per-store policy
  (``ownership_denial`` in the storefront policy): ``not_found`` (404, default —
  foreign resources are indistinguishable from nonexistent ones) or
  ``forbidden`` (403). It is never per-request.
"""

from __future__ import annotations

import json
import re
import secrets
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .installations import get_installation_by_id
from .storage import digest, encode

KINDS = {"guest", "buyer", "service"}
DENIALS = {"not_found", "forbidden"}
_SUBJECT = re.compile(r"^[A-Za-z0-9_:.\-/]{1,200}$")


def generate_pairwise_id(kind: str = "guest") -> str:
    """128-bit random pairwise principal id; never derived from raw PII."""
    if kind not in KINDS:
        raise ValueError("Unsupported principal kind")
    return kind + "_" + secrets.token_hex(16)


def _validate_subject(external_subject: str):
    if not isinstance(external_subject, str) or not _SUBJECT.fullmatch(external_subject):
        raise ValueError("External subject must be 1-200 safe characters")


def get_binding(dbs, installation_id: str, external_subject: str):
    with dbs.db() as db:
        row = db.execute(
            "SELECT * FROM principal_bindings WHERE installation_id=? AND external_subject=?",
            (installation_id, external_subject),
        ).fetchone()
    return _view(row) if row else None


def _view(row) -> dict:
    return {
        "installation_id": row["installation_id"],
        "external_subject": row["external_subject"],
        "merchant_principal": row["merchant_principal"],
        "kind": row["kind"],
        "scopes": json.loads(row["scopes"]),
        "consent_reference": row["consent_reference"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


def resolve_principal(dbs, installation_id: str, external_subject: str, *,
                      kind: str = "guest", scopes=None, create: bool = True):
    """Resolve (and by default create) the binding for an installation + subject.

    A revoked binding is returned as-is: revocation sticks and callers must deny.
    Only an explicit re-link with fresh consent reactivates it.
    """
    _validate_subject(external_subject)
    if kind not in KINDS:
        raise ValueError("Unsupported principal kind")
    existing = get_binding(dbs, installation_id, external_subject)
    if existing or not create:
        return existing
    try:
        with dbs.db() as db:
            db.execute(
                "INSERT INTO principal_bindings(installation_id,external_subject,merchant_principal,"
                "kind,scopes,consent_reference,created_at,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (installation_id, external_subject, generate_pairwise_id(kind), kind,
                 encode(sorted(scopes or [])), None, time.time()),
            )
    except dbs.integrity_errors:
        pass  # Concurrent resolve won the race; read the winner's binding.
    return get_binding(dbs, installation_id, external_subject)


def link_principal(dbs, installation_id: str, external_subject: str, *,
                   consent_reference: str, scopes=None):
    """Create or upgrade a binding to a verified buyer account link.

    Requires an explicit consent reference (proof the buyer authorized the
    link). Upgrading a guest binding mints a fresh ``buyer_`` pairwise principal
    and transfers fallback ownership records to it (account-link merge);
    re-linking a revoked buyer binding reactivates the same principal under a
    new consent reference.
    """
    _validate_subject(external_subject)
    if not isinstance(consent_reference, str) or not (1 <= len(consent_reference) <= 500):
        raise ValueError("Account linking requires a consent reference")
    now = time.time()
    with dbs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM principal_bindings WHERE installation_id=? AND external_subject=?",
            (installation_id, external_subject),
        ).fetchone()
        if row:
            principal_id = row["merchant_principal"]
            if row["kind"] == "guest":
                principal_id = generate_pairwise_id("buyer")
                db.execute(
                    "UPDATE resource_owners SET principal_id=? WHERE installation_id=? AND principal_id=?",
                    (principal_id, installation_id, row["merchant_principal"]),
                )
            db.execute(
                "UPDATE principal_bindings SET merchant_principal=?,kind='buyer',scopes=?,"
                "consent_reference=?,revoked_at=NULL WHERE installation_id=? AND external_subject=?",
                (principal_id, encode(sorted(scopes or json.loads(row["scopes"]))), consent_reference,
                 installation_id, external_subject),
            )
        else:
            db.execute(
                "INSERT INTO principal_bindings(installation_id,external_subject,merchant_principal,"
                "kind,scopes,consent_reference,created_at,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (installation_id, external_subject, generate_pairwise_id("buyer"), "buyer",
                 encode(sorted(scopes or [])), consent_reference, now),
            )
    return get_binding(dbs, installation_id, external_subject)


def unlink_principal(dbs, installation_id: str, external_subject: str) -> bool:
    """Revoke a binding. Resolution keeps returning it (revoked) so a fresh
    guest identity is never silently issued for a revoked subject."""
    with dbs.db() as db:
        changed = db.execute(
            "UPDATE principal_bindings SET revoked_at=? WHERE installation_id=? AND external_subject=? "
            "AND revoked_at IS NULL",
            (time.time(), installation_id, external_subject),
        ).rowcount
    return bool(changed)


def record_resource_owner(dbs, installation_id: str, resource_type: str,
                          resource_id: str, principal_id: str) -> str:
    """Record fallback ownership; returns 'recorded' | 'owned' | 'conflict'."""
    try:
        with dbs.db() as db:
            db.execute(
                "INSERT INTO resource_owners(installation_id,resource_type,resource_id,principal_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (installation_id, resource_type, resource_id, principal_id, time.time()),
            )
        return "recorded"
    except dbs.integrity_errors:
        existing = resource_owner(dbs, installation_id, resource_type, resource_id)
        return "owned" if existing == principal_id else "conflict"


def resource_owner(dbs, installation_id: str, resource_type: str, resource_id: str):
    with dbs.db() as db:
        row = db.execute(
            "SELECT principal_id FROM resource_owners WHERE installation_id=? AND resource_type=? AND resource_id=?",
            (installation_id, resource_type, resource_id),
        ).fetchone()
    return row["principal_id"] if row else None


def assert_owner(dbs, installation_id: str, resource_type: str, resource_id: str,
                 principal: str, *, denial: str = "not_found") -> bool:
    """Enforce fallback ownership before any transport call.

    A missing record means the merchant's native ownership model is
    authoritative and the call proceeds; a mismatched record is denied with the
    store's FIXED denial policy (404 'not_found' by default, else 403).
    """
    if denial not in DENIALS:
        raise ValueError("Unsupported ownership denial policy")
    owner = resource_owner(dbs, installation_id, resource_type, resource_id)
    if owner is None or owner == principal:
        return True
    if denial == "forbidden":
        raise HTTPException(403, "Resource belongs to a different principal")
    raise HTTPException(404, "Resource not found")


class ResolveBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_subject: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="guest")
    scopes: list[str] = Field(default_factory=list, max_length=25)


class LinkBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_subject: str = Field(min_length=1, max_length=200)
    consent_reference: str = Field(min_length=1, max_length=500)
    scopes: list[str] = Field(default_factory=list, max_length=25)


class UnlinkBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_subject: str = Field(min_length=1, max_length=200)


def attach_principals(app, dbs):
    state = app.state
    prefix = "/api/commerce/stores/{store_id}/installations/{installation_id}/principals"

    def owned_installation(store_id, installation_id, actor):
        state.owned(store_id, actor)
        row = get_installation_by_id(dbs, installation_id)
        if not row or row["store_id"] != store_id:
            raise HTTPException(404, "Installation not found for this store")
        return row

    @app.get(prefix)
    def list_bindings(store_id: str, installation_id: str, actor=Depends(state.user_dependency)):
        owned_installation(store_id, installation_id, actor)
        with dbs.db() as db:
            rows = db.execute(
                "SELECT * FROM principal_bindings WHERE installation_id=? ORDER BY created_at",
                (installation_id,),
            ).fetchall()
        return {"bindings": [_view(row) for row in rows]}

    @app.post(prefix + "/resolve", status_code=200)
    def resolve(store_id: str, installation_id: str, body: ResolveBinding,
                actor=Depends(state.user_dependency)):
        owned_installation(store_id, installation_id, actor)
        try:
            existing = get_binding(dbs, installation_id, body.external_subject)
            binding = resolve_principal(
                dbs, installation_id, body.external_subject, kind=body.kind, scopes=body.scopes
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if existing is None:
            dbs.event(actor["org"], store_id, actor["id"], "principal.bound",
                      {"installation_id": installation_id, "kind": binding["kind"],
                       "subject_digest": digest(body.external_subject)})
        return binding

    @app.post(prefix + "/link")
    def link(store_id: str, installation_id: str, body: LinkBinding,
             actor=Depends(state.user_dependency)):
        owned_installation(store_id, installation_id, actor)
        try:
            binding = link_principal(
                dbs, installation_id, body.external_subject,
                consent_reference=body.consent_reference, scopes=body.scopes,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        dbs.event(actor["org"], store_id, actor["id"], "principal.linked",
                  {"installation_id": installation_id, "subject_digest": digest(body.external_subject),
                   "consent_digest": digest(body.consent_reference)})
        return binding

    @app.post(prefix + "/unlink")
    def unlink(store_id: str, installation_id: str, body: UnlinkBinding,
               actor=Depends(state.user_dependency)):
        owned_installation(store_id, installation_id, actor)
        try:
            changed = unlink_principal(dbs, installation_id, body.external_subject)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if not changed:
            raise HTTPException(404, "No active binding for this subject")
        dbs.event(actor["org"], store_id, actor["id"], "principal.unlinked",
                  {"installation_id": installation_id, "subject_digest": digest(body.external_subject)})
        return {"revoked": True, "external_subject": body.external_subject}
