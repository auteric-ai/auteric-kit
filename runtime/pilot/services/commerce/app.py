"""Private, single-node commerce control plane and outbound connector job transport."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from cryptography.fernet import Fernet, InvalidToken

from .adapter_manifest import activation_requirements, manifest_for
from .product import BY_OPERATION, CAPABILITIES, capability_view, platform_setup, protocol_input_schema
from .security import check_password, domain_name, fetch_discovery, password_hash
from .secrets import SQLiteCredentialVault
from .storage import Store, digest, encode, uid

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
READS = {"search_products", "get_product", "get_cart", "get_checkout", "get_order", "get_shipping_options", "lookup_products"}
ACTION_TYPES = {
    "search_products": "catalog.search",
    "lookup_products": "catalog.search",
    "get_product": "product.read",
    "create_cart": "cart.create",
    "get_cart": "cart.read",
    "add_to_cart": "cart.add_item",
    "update_cart_item": "cart.update_item",
    "remove_from_cart": "cart.remove_item",
    "replace_cart_items": "cart.update",
    "cancel_cart": "cart.cancel",
    "create_checkout": "checkout.create",
    "get_checkout": "checkout.read",
    "update_checkout": "checkout.update",
    "complete_checkout": "checkout.complete",
    "cancel_checkout": "checkout.cancel",
    "get_order": "order.read",
    "apply_discount_code": "discount.apply",
    "remove_discount_code": "discount.remove",
    "get_shipping_options": "fulfillment.options.read",
    "set_shipping_address": "fulfillment.address.set",
    "select_shipping_option": "fulfillment.option.select",
}


def native_input_for_dispatch(operation: str, data: dict) -> dict:
    """Translate the legacy Gateway model into the locked Native HTTP input."""
    native_input = dict(data)
    if operation == "search_products" and "query" in native_input:
        native_input["q"] = native_input.pop("query")
    elif operation == "create_cart" and "items" in native_input:
        # The older public Gateway model calls the optional seed list `items`;
        # MEP/1 calls it `line_items`.
        native_input["line_items"] = [
            {key: value for key, value in item.items() if value is not None}
            for item in native_input.pop("items")
        ]
    elif operation == "create_checkout":
        # The generated MEP/1 contract owns only cart_id and optional revision
        # or buyer principal. These default-empty legacy fields are not part of
        # the signed merchant request.
        for field in ("buyer", "context", "payment", "fulfillment"):
            native_input.pop(field, None)
    elif operation in {"update_cart_item", "remove_from_cart"}:
        # Native contracts address a cart line, not a product selector.
        line_id = native_input.pop("line_id", None)
        native_input.pop("product_id", None)
        if not line_id:
            raise HTTPException(422, "Native cart line_id is required")
        native_input["line_id"] = line_id
    return native_input


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Model):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128)
    organization: str = Field(default="My organization", min_length=1, max_length=100)


class LoginCredentials(Model):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class GoogleCredential(Model):
    credential: str = Field(min_length=100, max_length=8192)


class NewStore(Model):
    name: str = Field(min_length=1, max_length=100)
    domain: str = Field(min_length=3, max_length=253)
    environment: Literal["dev", "sandbox", "staging", "production"] = "sandbox"
    platform: Literal["shopify", "wix", "woocommerce", "custom", "other"] = "custom"
    integration_mode: Literal["protect_existing", "enable_protect"] | None = None


class DetachStore(Model):
    """An explicit body prevents accidental detach requests."""
    confirm_store_id: str = Field(min_length=32, max_length=32)


class MappingDraft(Model):
    operation: str
    mapping: dict


class ContractTest(Model):
    input: dict = Field(default_factory=dict)
    mode: Literal["mock", "sandbox", "staging", "production_read_only"] = "sandbox"


class Poll(Model):
    store_id: str
    version: str = Field(max_length=40)
    environment: str = Field(max_length=30)
    release_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Result(Model):
    store_id: str
    result: Any = None
    error: dict | None = None


class CapabilityControl(Model):
    operation: str
    enabled: bool


class CapabilityControls(Model):
    capabilities: list[CapabilityControl] = Field(min_length=1, max_length=50)


class SetupMappings(Model):
    mappings: list[MappingDraft] = Field(min_length=1, max_length=50)


class AgentAccess(Model):
    enabled: bool


class UCPConfiguration(Model):
    payment_handlers: dict[str, list[dict]] = Field(default_factory=dict)
    identity_linking: dict | None = None


class CliStart(Model):
    challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")


class CliPoll(Model):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")
    verifier: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")


class LocalVerification(Model):
    store_url: str = Field(max_length=200)


def create_app(database=None, public_url=None, *, development=False, job_timeout=25):
    from auteric_commerce.domain import DomainError
    from auteric_edge.mapping import Mapping
    from auteric_edge.models import INPUTS
    from auteric_edge.models import validate_output as validate_response

    from .engine import StorefrontRuntime
    from .storefront_policy import StorefrontPolicy
    from .storefront_policy import evaluate as evaluate_policy

    app = FastAPI(title="Auteric Commerce Control Plane", version="0.1.0")
    dbs = Store(database or os.environ.get("DATABASE_URL") or os.environ.get("AUTERIC_COMMERCE_DATABASE", ".runtime/commerce/control.db"))
    vault = SQLiteCredentialVault(dbs)
    base = (public_url or os.environ.get("AUTERIC_COMMERCE_PUBLIC_URL", "http://127.0.0.1:8100")).rstrip("/")
    configured_job_key = os.environ.get("AUTERIC_JOB_ENCRYPTION_KEY")
    if configured_job_key:
        try:
            job_cipher = Fernet(configured_job_key.encode())
        except (ValueError, TypeError):
            raise ValueError("AUTERIC_JOB_ENCRYPTION_KEY must be a Fernet key") from None
    elif development:
        derived = base64.urlsafe_b64encode(hashlib.sha256((dbs.database_url + base).encode()).digest())
        job_cipher = Fernet(derived)
    else:
        job_cipher = None

    def encode_job_input(operation, value):
        if operation != "complete_checkout":
            return encode(value)
        if job_cipher is None:
            raise HTTPException(503, "Checkout completion requires AUTERIC_JOB_ENCRYPTION_KEY")
        return "fernet:v1:" + job_cipher.encrypt(encode(value).encode()).decode()

    def decode_job_input(operation, value):
        if operation != "complete_checkout":
            return json.loads(value)
        if not value.startswith("fernet:v1:") or job_cipher is None:
            raise HTTPException(503, "Encrypted checkout completion input is unavailable")
        try:
            return json.loads(job_cipher.decrypt(value.removeprefix("fernet:v1:").encode()).decode())
        except (InvalidToken, ValueError, json.JSONDecodeError):
            raise HTTPException(503, "Encrypted checkout completion input is invalid") from None
    if not development and urlsplit(base).scheme != "https":
        raise ValueError("Production control plane requires an explicit HTTPS public URL")
    app.state.store = dbs

    @app.on_event("shutdown")
    def close_store_pool():
        # PostgreSQL uses a bounded pool for Console and Gateway queries. Close
        # idle sockets during a graceful ECS/task shutdown.
        dbs.close()
    app.state.development = development
    app.state.public_url = base
    mcp_base = os.environ.get("AUTERIC_MCP_PUBLIC_URL", base).rstrip("/")
    mcp_origin = urlsplit(mcp_base)
    if (mcp_origin.scheme != "https" and not (development and mcp_origin.scheme == "http" and mcp_origin.hostname in {"127.0.0.1", "localhost", "::1"})) or not mcp_origin.hostname or mcp_origin.username or mcp_origin.password or mcp_origin.path or mcp_origin.query or mcp_origin.fragment:
        raise ValueError("MCP public URL must be an HTTPS origin (loopback HTTP in development)")
    app.state.mcp_public_url = mcp_base
    installation_mcp_base = os.environ.get("AUTERIC_INSTALLATION_MCP_PUBLIC_URL", base).rstrip("/")
    install_origin = urlsplit(installation_mcp_base)
    if (install_origin.scheme != "https" and not (development and install_origin.scheme == "http" and install_origin.hostname in {"127.0.0.1", "localhost", "::1"})) or not install_origin.hostname or install_origin.username or install_origin.password or install_origin.path or install_origin.query or install_origin.fragment:
        raise ValueError("Installation MCP public URL must be an HTTPS origin (loopback HTTP in development)")
    app.state.installation_mcp_public_url = installation_mcp_base
    prefix = "/api/commerce"

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def security_headers(request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 2_000_000:
            return JSONResponse({"detail": "Request exceeds the 2 MB limit"}, status_code=413)
        # Enforce a bound for chunked requests as well; downstream reuses cached body.
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2_000_000:
                    return JSONResponse({"detail": "Request exceeds the 2 MB limit"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )
        return response

    def origin(request):
        allowed = base
        if request.headers.get("origin") not in {None, allowed}:
            raise HTTPException(403, "Cross-origin requests are refused")
        if request.method not in {"GET", "HEAD"} and request.headers.get("x-auteric-console") != "1":
            raise HTTPException(403, "Console CSRF header required")

    def user(request: Request):
        origin(request)
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.cookies.get("auteric_session", "")
        with dbs.db() as db:
            row = db.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE sessions.token=? AND sessions.expires>?",
                (digest(token), time.time()),
            ).fetchone()
        if not row:
            raise HTTPException(401, "Sign in to your organization")
        return dict(row)

    def owned(store_id, actor):
        with dbs.db() as db:
            row = db.execute("SELECT * FROM stores WHERE id=? AND org=?", (store_id, actor["org"])).fetchone()
        if not row:
            raise HTTPException(404, "Store not found in your organization")
        return dict(row)

    def store_row(store_id):
        with dbs.db() as db:
            row = db.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown store")
        return dict(row)

    def auth_token(request, kind, store_id=None):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        row = vault.resolve(token, kind, store_id)
        if not row:
            raise HTTPException(401, "Invalid or revoked store credential")
        return row

    def session_response(actor):
        token = create_session(actor)
        response = JSONResponse({"id": actor["id"], "email": actor["email"], "organization": actor["name"]})
        set_session_cookie(response, token)
        return response

    def create_session(actor):
        token = secrets.token_urlsafe(40)
        with dbs.db() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), actor["id"], time.time() + 8 * 3600))
        return token

    def set_session_cookie(response, token):
        response.set_cookie(
            "auteric_session", token, httponly=True, secure=not development or os.getenv("AUTERIC_FORCE_SECURE_COOKIE") == "1", samesite="strict", max_age=8 * 3600
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "service": "commerce-control-plane",
            "mode": "local-development" if development else ("distributed" if dbs.postgres else "single-node-pilot"),
        }

    @app.post(prefix + "/cli/start")
    def cli_start(body: CliStart, request: Request):
        if not dbs.limit("cli:start:" + (request.client.host if request.client else "unknown"), 12, 600):
            raise HTTPException(429, "Too many authorization attempts")
        request_id = secrets.token_urlsafe(32)
        user_code = "-".join((str(secrets.randbelow(10 ** 4)).zfill(4) for _ in range(2)))
        expires = time.time() + 300
        with dbs.db() as db:
            db.execute(
                "INSERT INTO cli_authorizations(id,challenge,state_hash,exchange_hash,expires,created) VALUES(?,?,?,?,?,?)",
                (digest(request_id), body.challenge, digest(body.state), digest(user_code), expires, time.time()),
            )
        return {"request_id": request_id, "authorization_url": base + "/cli/authorize?request=" + request_id,
                "user_code": user_code, "expires_at": expires, "interval": 2}

    @app.get("/cli/authorize", response_class=HTMLResponse)
    def cli_authorize(request: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request):
            raise HTTPException(404, "Unknown authorization request")
        with dbs.db() as db:
            row = db.execute("SELECT expires,consumed FROM cli_authorizations WHERE id=?", (digest(request),)).fetchone()
        if not row or row["expires"] < time.time() or row["consumed"]:
            raise HTTPException(410, "Authorization expired")
        # The request ID is random and regex-constrained, never untrusted HTML.
        page = (Path(__file__).parent / "cli_authorize.html").read_text()
        return HTMLResponse(page.replace("__AUTERIC_REQUEST_ID__", request)
                            .replace("__REGISTRATION_ENABLED__", "true" if development else "false")
                            .replace("__LOCAL_DEVELOPMENT__", "true" if development else "false"))

    @app.post("/cli/authorize", response_class=HTMLResponse)
    def cli_authorize_script_required(request: str):
        """Fail closed if a browser submits the pairing form without its JS.

        The real form posts JSON to the auth API.  This route exists only to
        prevent a browser fallback from putting a password in the URL.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request):
            raise HTTPException(404, "Unknown authorization request")
        return HTMLResponse(
            "<!doctype html><title>JavaScript required</title>"
            "<p>Account setup requires JavaScript. Enable it and reopen the pairing page from your terminal.</p>",
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/cli/authorize.css")
    def cli_authorize_css():
        from fastapi.responses import Response
        return Response((Path(__file__).parent / "cli_authorize.css").read_text(), media_type="text/css")

    @app.get("/cli/authorize.js")
    def cli_authorize_js():
        from fastapi.responses import Response
        return Response((Path(__file__).parent / "cli_authorize.js").read_text(), media_type="text/javascript")

    @app.get("/auteric-mark.png", include_in_schema=False)
    def auteric_mark():
        """One shared brand mark for the console and browser pairing screen."""
        return FileResponse(ROOT / "apps" / "web" / "public" / "auteric-mark.png", media_type="image/png")

    @app.post(prefix + "/cli/approve")
    def cli_approve(body: dict, actor=Depends(user)):
        request_id = body.get("request_id", "")
        user_code = body.get("user_code", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request_id):
            raise HTTPException(422, "Invalid authorization request")
        if not isinstance(user_code, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{4}", user_code):
            raise HTTPException(422, "Enter the code shown in your terminal")
        if not dbs.limit("cli:approve:" + digest(request_id), 5, 300):
            raise HTTPException(429, "Too many incorrect codes; start again")
        with dbs.db() as db:
            changed = db.execute(
                "UPDATE cli_authorizations SET user_id=? WHERE id=? AND exchange_hash=? AND user_id IS NULL AND consumed IS NULL AND expires>?",
                (actor["id"], digest(request_id), digest(user_code), time.time()),
            ).rowcount
        if not changed:
            raise HTTPException(403, "Code does not match, or authorization expired")
        dbs.event(actor["org"], None, actor["id"], "cli.authorization.approved")
        return {"approved": True}

    @app.post(prefix + "/cli/poll")
    def cli_poll(body: CliPoll):
        import hashlib
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body.verifier.encode()).digest()).decode().rstrip("=")
        with dbs.db() as db:
            row = db.execute("SELECT * FROM cli_authorizations WHERE id=?", (digest(body.request_id),)).fetchone()
            if not row or row["expires"] < time.time() or row["consumed"]:
                raise HTTPException(410, "Authorization expired or used")
            if not secrets.compare_digest(row["challenge"], challenge) or not secrets.compare_digest(row["state_hash"], digest(body.state)):
                raise HTTPException(403, "Authorization session mismatch")
            if not row["user_id"]:
                return {"status": "pending"}
            # Single-use exchange; the bearer credential exists only after browser approval.
            changed = db.execute("UPDATE cli_authorizations SET consumed=? WHERE id=? AND consumed IS NULL", (time.time(), digest(body.request_id))).rowcount
            if not changed:
                raise HTTPException(410, "Authorization already used")
            actor = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        token = create_session(dict(actor))
        dbs.event(actor["org"], None, actor["id"], "cli.authorization.exchanged")
        return {"status": "authorized", "access_token": token, "expires_in": 8 * 3600,
                "user": {"email": actor["email"], "organization": actor["name"]}}

    @app.post(prefix + "/cli/complete")
    def cli_complete(body: dict, actor=Depends(user)):
        request_id = body.get("request_id")
        store_id = body.get("store_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request_id):
            raise HTTPException(422, "Invalid authorization request")
        if not isinstance(store_id, str) or not re.fullmatch(r"[a-f0-9]{32}", store_id):
            raise HTTPException(422, "Invalid store")
        owned(store_id, actor)
        with dbs.db() as db:
            row = db.execute("SELECT user_id,consumed,completed_store FROM cli_authorizations WHERE id=?",
                             (digest(request_id),)).fetchone()
            if not row or row["user_id"] != actor["id"] or not row["consumed"]:
                raise HTTPException(403, "Setup session was not approved by this account")
            if row["completed_store"] and row["completed_store"] != store_id:
                raise HTTPException(409, "Setup session already completed for another store")
            db.execute("UPDATE cli_authorizations SET completed_store=? WHERE id=?",
                       (store_id, digest(request_id)))
        return {"completed": True, "store_id": store_id}

    @app.get(prefix + "/cli/status")
    def cli_status(request: str, actor=Depends(user)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request):
            raise HTTPException(404, "Unknown authorization request")
        with dbs.db() as db:
            row = db.execute("SELECT user_id,consumed,completed_store FROM cli_authorizations WHERE id=?",
                             (digest(request),)).fetchone()
        if not row or row["user_id"] != actor["id"]:
            raise HTTPException(403, "Setup session belongs to another account")
        return {"status": "complete" if row["completed_store"] else "pending",
                "approved": bool(row["user_id"]), "store_id": row["completed_store"]}

    @app.post(prefix + "/auth/register")
    def register(body: Credentials, request: Request):
        origin(request)
        if not development:
            raise HTTPException(403, "Password registration is limited to local development. Use a verified identity provider.")
        if not dbs.limit("register:" + (request.client.host if request.client else "unknown"), 5, 600):
            raise HTTPException(429, "Too many account attempts; try later")
        email = body.email.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HTTPException(422, "Enter a valid email address")
        actor = {"id": uid(), "email": email, "org": uid(), "name": body.organization}
        try:
            with dbs.db() as db:
                db.execute(
                    "INSERT INTO users VALUES(?,?,?,?,?)",
                    (actor["id"], email, password_hash(body.password), actor["org"], body.organization),
                )
        except dbs.integrity_errors:
            raise HTTPException(409, "An account already exists for this email. Sign in instead.") from None
        dbs.event(actor["org"], None, actor["id"], "organization.created")
        return session_response(actor)

    @app.post(prefix + "/auth/login")
    def login(body: LoginCredentials, request: Request):
        origin(request)
        key = "login:" + (request.client.host if request.client else "unknown")
        if not dbs.limit(key, 10, 300):
            raise HTTPException(429, "Too many sign-in attempts; try later")
        with dbs.db() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (body.email.strip().lower(),)).fetchone()
        if not row or not row["password"] or not check_password(body.password, row["password"]):
            raise HTTPException(401, "Email or password is incorrect")
        return session_response(dict(row))

    @app.get(prefix + "/auth/providers")
    def auth_providers():
        client_id = os.getenv("AUTERIC_GOOGLE_CLIENT_ID", "").strip()
        return {"google_client_id": client_id or None,
                "local_registration": development}

    @app.post(prefix + "/auth/google")
    async def google_login(body: GoogleCredential, request: Request):
        origin(request)
        client_id = os.getenv("AUTERIC_GOOGLE_CLIENT_ID", "").strip()
        if not client_id:
            raise HTTPException(503, "Google sign-in is not configured")
        if not dbs.limit("google:login:" + (request.client.host if request.client else "unknown"), 15, 300):
            raise HTTPException(429, "Too many sign-in attempts")
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token
            claims = await asyncio.to_thread(id_token.verify_oauth2_token, body.credential, GoogleRequest(), client_id)
        except Exception:
            raise HTTPException(401, "Google identity could not be verified") from None
        subject = claims.get("sub")
        email = str(claims.get("email") or "").strip().lower()
        if claims.get("email_verified") is not True or not isinstance(subject, str) or not subject or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HTTPException(401, "A verified Google email is required")
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute("SELECT user_id FROM google_identities WHERE subject=?", (subject,)).fetchone()
            if identity:
                actor = db.execute("SELECT * FROM users WHERE id=?", (identity["user_id"],)).fetchone()
                if not actor:
                    raise HTTPException(409, "Google account link is invalid")
            else:
                if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    raise HTTPException(409, "This email already has an Auteric account. Sign in with its password; automatic account linking is disabled.")
                actor = {"id": uid(), "email": email, "org": uid(), "name": "My organization"}
                db.execute("INSERT INTO users VALUES(?,?,?,?,?)",
                           (actor["id"], email, None, actor["org"], actor["name"]))
                db.execute("INSERT INTO google_identities(subject,user_id,email,created) VALUES(?,?,?,?)",
                           (subject, actor["id"], email, time.time()))
        dbs.event(actor["org"], None, actor["id"], "auth.google.signed_in")
        return session_response(dict(actor))

    @app.post(prefix + "/auth/google/link")
    async def link_google_identity(body: GoogleCredential, request: Request, actor=Depends(user)):
        """Explicitly link Google only after the owner authenticated locally.

        A matching email alone is never sufficient: that would let any Google
        identity that claims an address take over an existing workspace.
        """
        origin(request)
        client_id = os.getenv("AUTERIC_GOOGLE_CLIENT_ID", "").strip()
        if not client_id:
            raise HTTPException(503, "Google sign-in is not configured")
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token
            claims = await asyncio.to_thread(id_token.verify_oauth2_token, body.credential, GoogleRequest(), client_id)
        except Exception:
            raise HTTPException(401, "Google identity could not be verified") from None
        subject = claims.get("sub")
        email = str(claims.get("email") or "").strip().lower()
        if claims.get("email_verified") is not True or not isinstance(subject, str) or not subject:
            raise HTTPException(401, "A verified Google email is required")
        if email != actor["email"].strip().lower():
            raise HTTPException(409, "Google email must match the signed-in Auteric account")
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT user_id FROM google_identities WHERE subject=?", (subject,)).fetchone()
            if existing and existing["user_id"] != actor["id"]:
                raise HTTPException(409, "This Google account is already linked to another Auteric account")
            if not existing:
                db.execute("INSERT INTO google_identities(subject,user_id,email,created) VALUES(?,?,?,?)",
                           (subject, actor["id"], email, time.time()))
        dbs.event(actor["org"], None, actor["id"], "auth.google.linked")
        return {"id": actor["id"], "email": actor["email"], "organization": actor["name"], "linked": True}

    @app.get(prefix + "/auth/me")
    def me(actor=Depends(user)):
        return {"id": actor["id"], "email": actor["email"], "organization": actor["name"]}

    @app.post(prefix + "/auth/logout")
    def logout(request: Request, actor=Depends(user)):
        with dbs.db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (digest(request.cookies.get("auteric_session", "")),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("auteric_session")
        return response

    def mapping_rows(store_id):
        with dbs.db() as db:
            return [
                {**dict(row), "mapping": json.loads(row["body"]), "tests": json.loads(row["tests"] or "null")}
                for row in db.execute("SELECT * FROM mappings WHERE store=? ORDER BY created DESC", (store_id,))
            ]

    def enabled_operations(store_id):
        with dbs.db() as db:
            return {
                row["operation"]
                for row in db.execute(
                    "SELECT operation FROM capability_controls WHERE store=? AND enabled=1", (store_id,)
                )
            }

    def active_operations(store_id, *, exposed=False, operation=None):
        # Native HTTP installations intentionally have no generated mapping
        # rows: their immutable registration plus per-operation evidence is
        # the adapter. Do not force this path through the legacy connector
        # mapping surface merely to make it visible to Gateway/MCP.
        from .installations import get_installation

        mapping_operations = {row["operation"] for row in mapping_rows(store_id) if row["state"] == "active"}
        # Preserve the legacy mapping path for migrated installations that
        # still have active mapping records.
        if mapping_operations:
            return mapping_operations & enabled_operations(store_id) if exposed else mapping_operations

        store = store_row(store_id)
        installation = get_installation(dbs, store_id, store["environment"])
        if installation and installation["transport"] == "native_http":
            with dbs.db() as db:
                installed = {
                    row["operation"] for row in db.execute(
                        "SELECT operation FROM installation_operations WHERE installation_id=?",
                        (installation["id"],),
                    )
                }
            if not exposed:
                return installed
            if operation is not None:
                # A dispatch checks one capability with the same durable
                # predicate used by the full listing. Scanning every registry
                # operation here can exhaust the execution window on PostgreSQL.
                from .capabilities import capability_enabled
                return {operation} if operation in installed and capability_enabled(dbs, store, operation) else set()
            from .capabilities import effective_listing

            return {
                item["operation"] for item in effective_listing(dbs, store)
                if item["enabled"]
            }
        return set()

    def operation_enabled(store_id, operation):
        return operation in active_operations(store_id, exposed=True, operation=operation)

    def connector_source(store):
        return 'shopify_hosted' if store['platform'] == 'shopify' else 'generated_merchant_connector'

    def hosted_connector_ready(store):
        return bool(
            store['platform'] == 'shopify'
            and getattr(app.state, 'shopify_hosted_ready', lambda _store: False)(store['id'])
        )

    def require_agent_access(store_id):
        app.state.require_gateway_running()
        store = store_row(store_id)
        if store['platform'] == 'shopify':
            app.state.require_shopify_installation(store_id)
        if not store["agent_access_enabled"]:
            raise HTTPException(409, "Agent access is disabled for this Store")
        with dbs.db() as db:
            installation_row = db.execute(
                "SELECT id,transport,revoked_at FROM installations WHERE store_id=? AND environment=?",
                (store_id, store["environment"]),
            ).fetchone()
        if installation_row and installation_row["transport"] == "native_http":
            # The heartbeat rule applies only to outbound_worker connections.
            # Native installations are reachable unless revoked or circuit-open.
            if installation_row["revoked_at"]:
                raise HTTPException(409, "Native installation is revoked")
            transport = getattr(app.state, "native_transport", None)
            if transport is not None and transport.circuit_open(installation_row["id"]):
                raise HTTPException(409, "Native installation circuit is open after repeated failures")
        else:
            hosted = hosted_connector_ready(store)
            if not hosted and (not store["heartbeat"] or store["heartbeat"] < time.time() - 40):
                raise HTTPException(409, "A current connector heartbeat is required")
            if not hosted and not str(store["connector_version"] or "").startswith("0.1."):
                raise HTTPException(409, "Connector version is incompatible")
        # Store-scoped sandbox acceptance uses the managed /ucp/{store_id}
        # ingress and must not pretend to own the merchant hostname. Public or
        # production traffic always requires a current verified routing binding.
        if store["environment"] == "production":
            try:
                resolved = resolve_public_store(store["domain"])
            except HTTPException:
                raise HTTPException(409, "Current verified Store routing is required") from None
            if resolved["id"] != store_id:
                raise HTTPException(409, "Verified hostname belongs to a different Store")
        return store

    def view(store):
        versions = mapping_rows(store["id"])
        active = [m for m in versions if m["state"] == "active"]
        enabled = enabled_operations(store["id"])
        hosted = hosted_connector_ready(store)
        from .installations import get_installation
        installation = get_installation(dbs, store["id"], store["environment"])
        native = bool(installation and installation["transport"] == "native_http" and not installation["revoked_at"])
        online = hosted or native or bool(store["heartbeat"] and store["heartbeat"] > time.time() - 40)
        verified = bool(store["verified"] and store["verified"] > time.time() - 86400)
        # Summary badges must use the same evidence as the activation screen.
        ready = health_view(store, {"name": "Store owner"})["protection"] == "active"
        result = {k: v for k, v in store.items() if k not in {"verification_token", "policy"}}
        result.update(
            connector={
                "status": "online" if online else "offline",
                "version": store["connector_version"],
                "last_heartbeat": store["heartbeat"],
                "environment": store["environment"],
                "mode": "native_http" if native else "auteric_hosted" if hosted else "merchant_managed",
                "merchant_setup_required": not hosted and not native,
            },
            policy=json.loads(store["policy"]),
            mappings=versions,
            capabilities=capability_view(versions, enabled, source=connector_source(store)),
            active_capabilities=sorted({m["operation"] for m in active} & enabled),
            discovery_verified=verified,
            agent_ready=ready,
            protected=ready,
            production_ready=False,
            status="sandbox-ready"
            if ready and store["environment"] != "production"
            else "validated-pilot"
            if ready
            else "setup-required",
            scanner_url="https://scanner.auteric.com/",
            scanner_findings=None,
            platform_setup=platform_setup(store["platform"]),
        )
        with dbs.db() as db:
            rows = db.execute(
                "SELECT operation,decision,state,latency,session,agent FROM traffic WHERE store=? AND created>?",
                (store["id"], time.time() - 86400),
            ).fetchall()
        result["metrics"] = {
            "requests": len(rows),
            "sessions": len({r["session"] for r in rows}),
            "agents": len({r["agent"] for r in rows}),
            "blocked": sum(r["state"] == "blocked" for r in rows),
            "errors": sum(r["state"] in {"failed", "uncertain"} for r in rows),
            "catalog_searches": sum(r["operation"] == "search_products" for r in rows),
            "cart_operations": sum("cart" in r["operation"] for r in rows),
            "checkout_attempts": sum(r["operation"] == "create_checkout" for r in rows),
            "average_latency_ms": round(sum(r["latency"] or 0 for r in rows) / len(rows), 1) if rows else 0,
        }
        return result

    @app.get(prefix + "/stores")
    def stores(actor=Depends(user)):
        with dbs.db() as db:
            rows = db.execute("SELECT * FROM stores WHERE org=? ORDER BY created DESC", (actor["org"],)).fetchall()
        return [view(dict(row)) for row in rows]

    @app.post(prefix + "/stores", status_code=201)
    def create_store(body: NewStore, actor=Depends(user)):
        if body.environment=='dev' and not development:
            raise HTTPException(422,'Dev stores require a local development control plane')
        try:
            domain = domain_name(body.domain)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        store_id = uid()
        integration_mode = body.integration_mode or (
            "enable_protect" if body.platform in {"custom", "other"} else "protect_existing"
        )
        with dbs.db() as db:
            db.execute(
                "INSERT INTO stores(id,org,name,domain,environment,verification_token,verified,heartbeat,"
                "connector_version,policy,created,platform,integration_mode,agent_access_enabled,verification_method) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    store_id,
                    actor["org"],
                    body.name,
                    domain,
                    body.environment,
                    secrets.token_urlsafe(32),
                    None,
                    None,
                    None,
                    StorefrontPolicy().model_dump_json(),
                    time.time(),
                    body.platform,
                    integration_mode,
                    0,
                    None,
                ),
            )
        dbs.event(
            actor["org"],
            store_id,
            actor["id"],
            "store.created",
            {"environment": body.environment, "platform": body.platform, "integration_mode": integration_mode},
        )
        return view(owned(store_id, actor))

    @app.post(prefix + "/stores/{store_id}/detach")
    def detach_store(store_id: str, body: DetachStore, actor=Depends(user)):
        """Detach without deleting merchant data, but fail closed for agents."""
        if body.confirm_store_id != store_id:
            raise HTTPException(422, "Confirm the exact Store before detaching")
        store = owned(store_id, actor)
        now = time.time()
        with dbs.db() as db:
            db.execute("UPDATE credentials SET revoked=? WHERE store=? AND revoked IS NULL", (now, store_id))
            db.execute("DELETE FROM agent_sessions WHERE store=?", (store_id,))
            changed = db.execute(
                "UPDATE stores SET org=NULL,agent_access_enabled=0,verified=NULL,detached_at=?,detached_by=? "
                "WHERE id=? AND org=?",
                (now, actor["id"], store_id, actor["org"]),
            ).rowcount
        if not changed:
            raise HTTPException(409, "Store was already detached")
        dbs.event(actor["org"], store_id, actor["id"], "store.detached", {"domain": store["domain"]})
        return {"detached": True, "store_id": store_id, "agent_access_disabled": True, "credentials_revoked": True}

    @app.get(prefix + "/stores/{store_id}")
    def get_store(store_id: str, actor=Depends(user)):
        return view(owned(store_id, actor))

    @app.get(prefix + "/local-stores/{store_id}/readiness")
    def local_store_readiness(store_id: str, request: Request):
        """Expose only non-secret local sandbox evidence to the local Scanner."""
        host = request.client.host if request.client else ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not development or not is_loopback:
            raise HTTPException(404, "Local readiness is unavailable")
        store = store_row(store_id)
        mappings = mapping_rows(store_id)
        enabled = enabled_operations(store_id)
        active = sorted({row["operation"] for row in mappings if row["state"] == "active"} & enabled)
        fresh = bool(store["heartbeat"] and store["heartbeat"] > time.time() - 40)
        return {
            "mode": "local_sandbox",
            "store_id": store_id,
            "domain": store["domain"],
            "connector_online": fresh,
            "agent_access_enabled": bool(store["agent_access_enabled"]),
            "active_operations": active,
            "connected": bool(fresh and store["agent_access_enabled"] and active),
            "production_verified": False,
        }

    @app.get(prefix + "/local-stores/{store_id}/catalog")
    async def local_store_catalog(store_id: str, request: Request):
        """Bounded read-only catalog bridge for the locally bound Scanner only."""
        host = request.client.host if request.client else ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not development or not is_loopback:
            raise HTTPException(404, "Local catalog bridge is unavailable")
        store = store_row(store_id)
        if not store["agent_access_enabled"] or not store["heartbeat"] or store["heartbeat"] <= time.time() - 40:
            raise HTTPException(409, "A current local connector is required")
        if not operation_enabled(store_id, "search_products"):
            raise HTTPException(409, "Catalog search is not enabled")
        mapping = selected_mapping(store_id, operation="search_products")
        result = await dispatch(store_id, "search_products", {"query": "", "limit": 50}, mapping)
        if not isinstance(result, list) or len(result) > 50:
            raise HTTPException(502, "Connector returned an invalid catalog response")
        return {"source": "tested_auteric_connector", "products": result}

    def issue_token(store_id, kind, actor):
        store = owned(store_id, actor)
        token = secrets.token_urlsafe(40)
        vault.rotate(store_id, kind, token)
        dbs.event(actor["org"], store_id, actor["id"], kind + ".credential.rotated")
        return {
            "token": token,
            "credential_id": digest(token),
            "store_id": store_id,
            "api_url": base,
            "display_once": True,
            "installation": "python -m pip install ./auteric-commerce-sdk",
            "command": "auteric-commerce connector start --shopify-installation" if store['platform'] == 'shopify' and kind == 'connector' else "auteric-commerce connector start",
            "environment_variables": {
                "AUTERIC_STORE_ID": store_id,
                "AUTERIC_API_URL": base,
                "AUTERIC_CONNECTOR_TOKEN": "<the token shown once>",
            },
        }

    @app.post(prefix + "/stores/{store_id}/connector-token")
    def connector_token(store_id: str, actor=Depends(user)):
        return issue_token(store_id, "connector", actor)

    @app.post(prefix + "/stores/{store_id}/agent-token")
    def agent_token(store_id: str, actor=Depends(user)):
        return issue_token(store_id, "agent", actor)

    def setup_authorization(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        with dbs.db() as db:
            row = db.execute(
                "SELECT * FROM setup_authorizations WHERE hash=? AND expires>? AND completed IS NULL",
                (digest(token), time.time()),
            ).fetchone()
        if not row:
            raise HTTPException(401, "Setup authorization is invalid, expired or already completed")
        return dict(row)

    @app.post(prefix + "/stores/{store_id}/setup-authorizations")
    def create_setup_authorization(store_id: str, actor=Depends(user)):
        store = owned(store_id, actor)
        encoded_api = base64.urlsafe_b64encode(base.encode()).decode().rstrip("=")
        token = "at_setup_" + encoded_api + "." + secrets.token_urlsafe(40)
        expires = time.time() + 15 * 60
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE setup_authorizations SET completed=? WHERE store=? AND completed IS NULL",
                (time.time(), store_id),
            )
            db.execute(
                "INSERT INTO setup_authorizations(hash,store,org,expires,completed,created) VALUES(?,?,?,?,NULL,?)",
                (digest(token), store_id, actor["org"], expires, time.time()),
            )
        dbs.event(actor["org"], store_id, actor["id"], "setup.authorization.created", {"expires": expires})
        return {
            "setup_token": token,
            "expires_at": expires,
            "store_id": store_id,
            "api_url": base,
            "display_once": True,
            "command": "auteric setup --authorization <token>",
            "scope": ["store.read", "mapping.propose", "setup.complete"],
            "permanent_credential": False,
        }

    @app.get(prefix + "/setup/context")
    def setup_context(authorization=Depends(setup_authorization)):
        store = store_row(authorization["store"])
        return {
            "managed_profile_url": base + '/ucp/' + store['id'] + '/.well-known/ucp',
            "store": {
                "id": store["id"],
                "name": store["name"],
                "domain": store["domain"],
                "platform": store["platform"],
                "environment": store["environment"],
                "integration_mode": store["integration_mode"],
            },
            "canonical_capabilities": [
                {
                    "operation": capability.operation,
                    "name": capability.name,
                    "description": capability.description,
                    "side_effect": capability.side_effect,
                }
                for capability in CAPABILITIES
            ],
            "expires_at": authorization["expires"],
        }

    @app.post(prefix + "/setup/mappings")
    def setup_mappings(body: SetupMappings, authorization=Depends(setup_authorization)):
        store = store_row(authorization["store"])
        actor = {"id": "setup-assistant", "org": store["org"]}
        rows = [save_mapping(store["id"], item.operation, item.mapping, actor) for item in body.mappings]
        return {"mappings": rows, "auto_activated": False, "review_required": True}

    @app.post(prefix + "/setup/complete")
    def complete_setup(authorization=Depends(setup_authorization)):
        with dbs.db() as db:
            db.execute(
                "UPDATE setup_authorizations SET completed=? WHERE hash=? AND completed IS NULL",
                (time.time(), authorization["hash"]),
            )
        dbs.event(authorization["org"], authorization["store"], "setup-assistant", "setup.authorization.completed")
        return {"completed": True, "store_id": authorization["store"], "credential_revoked": True}

    def save_mapping(store_id, operation, mapping, actor):
        try:
            candidate = Mapping.model_validate({**mapping, "operation": operation})
            body = candidate.model_dump(mode="json", exclude_unset=True)
        except Exception:
            raise HTTPException(
                422, "Mapping schema is invalid; review operation, method, path and transformations"
            ) from None
        version = uid()
        with dbs.db() as db:
            db.execute(
                "INSERT INTO mappings VALUES(?,?,?,?,?,?,?,?,NULL)",
                (version, store_id, operation, encode(body), "draft", digest(encode(body)), None, time.time()),
            )
        dbs.event(
            actor["org"], store_id, actor["id"], "mapping.generated", {"version": version, "operation": operation}
        )
        return next(m for m in mapping_rows(store_id) if m["id"] == version)

    app.state.save_mapping = save_mapping

    @app.post(prefix + "/stores/{store_id}/openapi")
    async def import_openapi(store_id: str, request: Request, actor=Depends(user)):
        from auteric_edge.mapping import suggest_mappings

        owned(store_id, actor)
        document = (await request.json()).get("document")
        try:
            suggestions = suggest_mappings(document)
        except Exception:
            raise HTTPException(
                422,
                "OpenAPI document could not be analyzed. Upload JSON OpenAPI 3.x; remote references are not fetched.",
            ) from None
        rows = []
        for suggestion in suggestions:
            saved = save_mapping(store_id, suggestion["operation"], suggestion["mapping"], actor)
            saved.update(confidence=suggestion.get("confidence"), rationale=suggestion.get("rationale"))
            rows.append(saved)
        return {
            "mappings": rows,
            "proposal_method": "deterministic heuristics; developer review required",
            "auto_activated": False,
        }

    @app.get(prefix + "/stores/{store_id}/mappings")
    @app.get(prefix + "/stores/{store_id}/mappingversions")
    def mappings(store_id: str, actor=Depends(user)):
        owned(store_id, actor)
        return mapping_rows(store_id)

    @app.get(prefix + "/stores/{store_id}/mappings/{version}/adapter-manifest")
    def adapter_manifest(store_id: str, version: str, actor=Depends(user)):
        owned(store_id, actor)
        mapping = selected_mapping(store_id, version=version)
        return {
            **manifest_for(mapping["operation"], mapping["fingerprint"]),
            "activation_requirements": activation_requirements(mapping["operation"]),
        }

    @app.post(prefix + "/stores/{store_id}/mappings")
    def draft(store_id: str, body: MappingDraft, actor=Depends(user)):
        owned(store_id, actor)
        return save_mapping(store_id, body.operation, body.mapping, actor)

    def selected_mapping(store_id, version=None, operation=None):
        # The native adapter is pinned by its installation binding digest, not
        # a mutable generated mapping. Gateway still receives a stable
        # descriptor for audit/replay bookkeeping, while execution is routed
        # exclusively through native_dispatch below.
        from .installations import get_installation, native_runtime_binding_digest

        store = store_row(store_id)
        installation = get_installation(dbs, store_id, store["environment"])
        if installation and installation["transport"] == "native_http" and operation:
            with dbs.db() as db:
                registered = db.execute(
                    "SELECT 1 FROM installation_operations WHERE installation_id=? AND operation=?",
                    (installation["id"], operation),
                ).fetchone()
            if registered:
                return {
                    "id": f"native:{installation['id']}:{operation}",
                    "operation": operation,
                    "body": "{}",
                    "fingerprint": native_runtime_binding_digest(installation),
                    "state": "active",
                }
        with dbs.db() as db:
            if version:
                row = db.execute("SELECT * FROM mappings WHERE store=? AND id=?", (store_id, version)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM mappings WHERE store=? AND operation=? AND state='active'", (store_id, operation)
                ).fetchone()
        if not row:
            raise HTTPException(409, "No tested active mapping for this operation; complete mapping setup")
        return dict(row)

    async def native_dispatch(installation_row, store, operation, data, mapping, *, key, principal=None,
                              operator_test=False):
        """Route execution through the MEP/1 native HTTP transport.

        Reached only after the shared dispatch policy/grant/audit path; the
        durable action record is written by the transport before dispatch. The
        external subject is bridged to a pairwise merchant principal (P1-09):
        the JWT `sub` is the pairwise id, never the raw subject, and fallback
        resource ownership is enforced before any transport call.
        """
        from . import principals
        from .installations import Installation, native_runtime_binding_digest
        from .transports.base import TransportAction
        from .transports.native_http import ERROR_HTTP, contracts

        transport = getattr(app.state, "native_transport", None)
        if transport is None:
            raise HTTPException(503, "Native HTTP execution signing is not configured")
        info = contracts().OPERATIONS.get(operation)
        if info is None:
            raise HTTPException(422, "Unsupported canonical operation")
        installation = Installation.from_row(installation_row)
        try:
            binding_digest = native_runtime_binding_digest(installation_row)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if principal is None:
            # Operator-driven contract tests get an ephemeral pairwise guest;
            # no binding row is persisted for them.
            merchant_principal = principals.generate_pairwise_id("guest")
        else:
            binding = principals.resolve_principal(dbs, installation.id, principal)
            if binding["revoked_at"]:
                raise HTTPException(403, "Principal binding was revoked for this installation")
            merchant_principal = binding["merchant_principal"]
        try:
            denial = StorefrontPolicy.model_validate_json(store["policy"]).ownership_denial
        except Exception:
            denial = "not_found"
        resource = data.get("cart_id") or data.get("checkout_id") or data.get("order_id")
        if resource:
            resource_kind = "order" if "order_id" in data else "checkout" if "checkout_id" in data else "cart"
            # Cross-buyer access is denied here, before the transport call.
            principals.assert_owner(
                dbs, installation.id, resource_kind, resource, merchant_principal, denial=denial
            )
        # The existing Gateway model predates the locked Native HTTP schema
        # and calls catalog search's text field `query`. Translate only at the
        # native transport boundary; the merchant sees the canonical `q`
        # contract and legacy callers retain their compatibility surface.
        native_input = native_input_for_dispatch(operation, data)
        action = TransportAction(
            operation=operation,
            input=native_input,
            principal=merchant_principal,
            # MEP action identifiers are explicit opaque values. Existing
            # Gateway routes derive a stable hex digest from idempotency keys;
            # wrap it without losing stability so the merchant SDK can reject
            # arbitrary IDs while legacy callers retain durable replay.
            action_id=key if key.startswith("action_") else "action_" + key,
            contract_version=info["contract_version"],
            # The runtime pins this registration-time digest. Mapping metadata
            # is evidence for the control plane, never an execution authority.
            binding_digest=binding_digest,
            operator_test=operator_test,
        )
        result = await transport.execute(installation, action)
        if result.outcome == "completed":
            if operation == "create_cart" and isinstance(result.result, dict) and result.result.get("cart_id"):
                principals.record_resource_owner(
                    dbs, installation.id, "cart", result.result["cart_id"], merchant_principal
                )
            elif operation == "create_checkout" and isinstance(result.result, dict) and result.result.get("checkout_id"):
                principals.record_resource_owner(
                    dbs, installation.id, "checkout", result.result["checkout_id"], merchant_principal
                )
            return result.result
        error = result.error or {}
        if result.outcome == "uncertain":
            raise HTTPException(
                504,
                "Merchant execution is uncertain; reconcile by action_id. "
                "Writes are never automatically retried.",
            )
        raise HTTPException(
            ERROR_HTTP.get(error.get("code"), 502),
            error.get("message") or "Merchant execution failed",
        )

    async def dispatch(store_id, operation, data, mapping, *, key=None, principal=None,
                       policy_exempt=False):
        """Shared dispatch. `policy_exempt` is reserved for operator-driven
        contract tests, which produce the test-evidence prerequisite and must
        run before enable; agent traffic is never exempt."""
        app.state.require_gateway_running()
        key = key or uid()
        with dbs.db() as db:
            routing_store = db.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
            installation_row = (
                db.execute(
                    "SELECT * FROM installations WHERE store_id=? AND environment=?",
                    (store_id, routing_store["environment"]),
                ).fetchone()
                if routing_store
                else None
            )
        if installation_row:
            if installation_row["revoked_at"]:
                raise HTTPException(409, "Native installation is revoked; execution is blocked")
            if installation_row["transport"] == "native_http":
                if not policy_exempt:
                    # Effective-policy enforcement on EVERY call (P1-16), also
                    # inside an open MCP session; tools/list filtering is not
                    # the enforcement mechanism. The worker transport keeps its
                    # existing per-call checks in execute_action.
                    from .capabilities import operation_states

                    states = operation_states(dbs, dict(routing_store), operation)
                    if not states["enabled"]:
                        raise HTTPException(
                            403,
                            f"Capability is disabled by the merchant effective policy "
                            f"(CAPABILITY_DISABLED, reason={states['reason']})",
                        )
                return await native_dispatch(
                    installation_row, dict(routing_store), operation, data, mapping, key=key,
                    principal=principal, operator_test=policy_exempt,
                )
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE id=? AND store=?", (key, store_id)).fetchone()
            if existing and (decode_job_input(operation, existing["input"]) != data or existing["mapping_version"] != mapping["id"]):
                raise HTTPException(409, "Connector job replay differs from the original request")
            if not existing:
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        store_id,
                        operation,
                        encode_job_input(operation, data),
                        mapping["body"],
                        mapping["id"],
                        "queued",
                        None,
                        None,
                        time.time(),
                        None,
                        time.time() + job_timeout,
                    ),
                )
        store = store_row(store_id)
        hosted = (
            store['platform'] == 'shopify'
            and getattr(app.state, 'shopify_hosted_ready', lambda _store: False)(store_id)
        )
        if hosted:
            # Claim the same durable job record used by outbound workers. A
            # process loss after claim remains uncertain and is never replayed.
            with dbs.db() as db:
                claimed = db.execute(
                    "UPDATE jobs SET state='claimed',claimed=? WHERE id=? AND store=? AND state='queued'",
                    (time.time(), key, store_id),
                ).rowcount
            if claimed:
                state, result, error = 'failed', None, None
                try:
                    result = await asyncio.wait_for(
                        app.state.shopify_hosted_execute(
                            store_id, operation, data, json.loads(mapping['body'])
                        ),
                        timeout=job_timeout,
                    )
                    normalized = validate_response(operation, result)
                    if hasattr(normalized, 'model_dump'):
                        normalized = normalized.model_dump(mode='json')
                    elif isinstance(normalized, list):
                        normalized = [
                            value.model_dump(mode='json') if hasattr(value, 'model_dump') else value
                            for value in normalized
                        ]
                    result, state = normalized, 'completed'
                except Exception:
                    # A failed read is safe to retry with a new action. A write
                    # may have reached Shopify, so keep it uncertain.
                    state = 'failed' if operation in READS else 'uncertain'
                    error = {
                        'code': 'hosted_connector_or_schema_error',
                        'message': 'Hosted Shopify execution was not confirmed',
                        'uncertain': state == 'uncertain',
                    }
                with dbs.db() as db:
                    db.execute(
                        "UPDATE jobs SET state=?,result=?,error=? WHERE id=? AND store=? AND state='claimed'",
                        (state, encode(result), encode(error), key, store_id),
                    )
        deadline = time.monotonic() + job_timeout
        while time.monotonic() < deadline:
            with dbs.db() as db:
                row = db.execute("SELECT * FROM jobs WHERE id=? AND store=?", (key, store_id)).fetchone()
            if row["state"] == "completed":
                return json.loads(row["result"])
            if row["state"] in {"failed", "uncertain"}:
                raise HTTPException(
                    502,
                    "Merchant connector failed or returned an ambiguous result. "
                    "Inspect the job; writes are not automatically retried.",
                )
            await asyncio.sleep(0.05)
        with dbs.db() as db:
            db.execute(
                "UPDATE jobs SET state=CASE WHEN state='queued' THEN 'failed' ELSE 'uncertain' END "
                "WHERE id=? AND state IN ('queued','claimed')",
                (key,),
            )
        raise HTTPException(
            504,
            "Connector did not confirm within the time limit. Check connectivity; do not replay an uncertain write.",
        )

    @app.post(prefix + "/edge/poll")
    def poll(body: Poll, request: Request):
        auth_token(request, "connector", body.store_id)
        store = store_row(body.store_id)
        if body.environment != store["environment"] or not body.version.startswith("0.1."):
            raise HTTPException(409, "Connector environment or version is incompatible with this store")
        if not dbs.limit("poll:" + body.store_id, 600):
            raise HTTPException(429, "Poll rate exceeded; back off")
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE stores SET heartbeat=?,connector_version=?,connector_release=? WHERE id=?",
                (time.time(), body.version, body.release_digest, body.store_id),
            )
            row = db.execute(
                "SELECT * FROM jobs WHERE store=? AND state='queued' AND expires>? ORDER BY created LIMIT 1"
                + (" FOR UPDATE SKIP LOCKED" if dbs.postgres else ""),
                (body.store_id, time.time()),
            ).fetchone()
            if row:
                app.state.require_gateway_running()
                # Stop queued agent traffic when its merchant is disabled. Setup
                # contract tests remain available because they have no traffic row.
                traffic = db.execute('SELECT agent,session FROM traffic WHERE id=? AND store=?', (row['id'], body.store_id)).fetchone()
                if traffic and not traffic['agent'].startswith('operator:'):
                    require_agent_access(body.store_id)
                    valid = db.execute('SELECT 1 FROM agent_sessions JOIN credentials ON credentials.hash=agent_sessions.agent '
                                       "WHERE agent_sessions.hash=? AND agent_sessions.store=? AND agent_sessions.agent=? AND agent_sessions.expires>? AND credentials.revoked IS NULL AND credentials.kind IN ('agent','mcp')",
                                       (traffic['session'], body.store_id, traffic['agent'], time.time())).fetchone()
                    if not valid:
                        db.execute("UPDATE jobs SET state='failed',error=? WHERE id=? AND state='queued'",
                                   (encode({'code': 'authority_revoked', 'uncertain': False}), row['id']))
                        row = None
                if row:
                    changed = db.execute("UPDATE jobs SET state='claimed',claimed=? WHERE id=? AND state='queued'", (time.time(), row["id"])).rowcount
                    if changed != 1:
                        row = None
        if not store["heartbeat"] or store["heartbeat"] < time.time() - 40:
            dbs.event(store["org"], store["id"], "connector", "connector.online", {"version": body.version})
        return {
            "job": None
            if not row
            else {
                "id": row["id"],
                "operation": row["operation"],
                "input": decode_job_input(row["operation"], row["input"]),
                "mapping": json.loads(row["mapping"]),
                "mapping_version": row["mapping_version"],
                "expires_at": row["expires"],
            }
        }

    @app.post(prefix + "/edge/jobs/{job_id}/result")
    def job_result(job_id: str, body: Result, request: Request):
        auth_token(request, "connector", body.store_id)
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND store=?" +
                             (" FOR UPDATE" if dbs.postgres else ""), (job_id, body.store_id)).fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            if row["state"] == "completed":
                if encode(body.result) != row["result"] or body.error:
                    raise HTTPException(409, "Conflicting completion receipt")
                return {"ok": True, "duplicate": True}
            if row["state"] not in {"claimed", "uncertain"}:
                raise HTTPException(409, "Job was not claimed or has already failed")
            error = None
            normalized = None
            try:
                if body.error:
                    raise ValueError("Connector error")
                normalized = validate_response(row["operation"], body.result)
                if hasattr(normalized, "model_dump"):
                    normalized = normalized.model_dump(mode="json")
                elif isinstance(normalized, list):
                    normalized = [v.model_dump(mode="json") if hasattr(v, "model_dump") else v for v in normalized]
                state = "completed"
            except Exception:
                state = "failed" if row["operation"] in READS else "uncertain"
                error = {
                    "code": "connector_or_schema_error",
                    "message": "Merchant response was not confirmed against the canonical schema",
                    "uncertain": state == "uncertain",
                }
            db.execute(
                "UPDATE jobs SET state=?,result=?,error=? WHERE id=?",
                (state, encode(normalized), encode(error), job_id),
            )
        return {"ok": True, "state": state}

    @app.post(prefix + "/stores/{store_id}/mappings/{version}/test")
    async def contract_test(store_id: str, version: str, body: ContractTest, actor=Depends(user)):
        store = owned(store_id, actor)
        mapping = selected_mapping(store_id, version=version)
        operation = mapping["operation"]
        if store["environment"] == "production" and (operation not in READS or body.mode != "production_read_only"):
            raise HTTPException(403, "Automatic production write tests are disabled. Use a sandbox/staging store.")
        if body.mode == "mock" or (store["environment"] != "production" and body.mode != store["environment"]):
            raise HTTPException(
                422,
                "Test environment must match the connected store; mock-only evidence cannot activate a live mapping",
            )
        try:
            data = INPUTS[operation].model_validate(body.input).model_dump(mode="json")
        except Exception:
            raise HTTPException(422, "Test input does not satisfy the canonical request schema") from None
        try:
            result = await dispatch(store_id, operation, data, mapping, policy_exempt=True)
            if store_row(store_id)['connector_release'] != store['connector_release']:
                raise HTTPException(409, 'Connector release changed during the contract test; retest this release')
            evidence = {
                "status": "pass",
                "environment": store["environment"],
                "fingerprint": mapping["fingerprint"],
                "tested_at": time.time(),
                "response": result,
                "checks": ["outbound connector connectivity", "merchant request", "canonical response schema"],
                "adapter_manifest": manifest_for(operation, mapping["fingerprint"]),
                "adapter_requirements": activation_requirements(operation),
                "production_ready": False,
                "connector_release": store["connector_release"],
            }
        except HTTPException as exc:
            evidence = {
                "status": "fail",
                "environment": store["environment"],
                "fingerprint": mapping["fingerprint"],
                "tested_at": time.time(),
                "error": str(exc.detail),
            }
        with dbs.db() as db:
            db.execute("UPDATE mappings SET tests=? WHERE id=? AND store=?", (encode(evidence), version, store_id))
        dbs.event(
            actor["org"], store_id, actor["id"], "mapping.tested", {"version": version, "status": evidence["status"]}
        )
        return evidence

    def validate_activation(store, mapping):
        evidence = json.loads(mapping["tests"] or "null")
        if evidence and evidence.get("kind") == "environment_promotion":
            app.state.validate_promotion(store, mapping, evidence)
        if (
            not evidence
            or evidence["status"] != "pass"
            or evidence["fingerprint"] != mapping["fingerprint"]
            or evidence["environment"] != store["environment"]
            or evidence["tested_at"] < time.time() - 86400
        ):
            raise HTTPException(
                409, "Fresh passing contract evidence for this exact version and environment is required"
            )
        expected_manifest = manifest_for(mapping["operation"], mapping["fingerprint"])
        observed_manifest = evidence.get("adapter_manifest")
        if not isinstance(observed_manifest, dict) or observed_manifest.get("digest") != expected_manifest["digest"]:
            raise HTTPException(
                409, "Adapter-manifest evidence is missing or does not match this exact mapping version"
            )
        return evidence

    def activate_mappings_atomic(store_id, versions, actor):
        store = owned(store_id, actor)
        if not versions or len(set(versions)) != len(versions):
            raise HTTPException(422, "Provide unique mapping versions")
        mappings = [selected_mapping(store_id, version=version) for version in versions]
        for mapping in mappings:
            validate_activation(store, mapping)
        activated = time.time()
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for mapping in mappings:
                db.execute(
                    "UPDATE mappings SET state='superseded' WHERE store=? AND operation=? AND state='active'",
                    (store_id, mapping["operation"]),
                )
                updated = db.execute(
                    "UPDATE mappings SET state='active',activated=? WHERE store=? AND id=? AND state='draft'",
                    (activated, store_id, mapping['id']),
                ).rowcount
                if updated != 1:
                    raise HTTPException(409, "Mapping state changed during activation")
                db.execute(
                    "INSERT INTO capability_controls(store,operation,enabled,updated) VALUES(?,?,1,?) "
                    "ON CONFLICT(store,operation) DO UPDATE SET enabled=1,updated=excluded.updated",
                    (store_id, mapping["operation"], activated),
                )
            db.execute("UPDATE stores SET verified=NULL WHERE id=?", (store_id,))
            db.execute("UPDATE routing_bindings SET verified=NULL WHERE store=?", (store_id,))
        for mapping in mappings:
            dbs.event(actor["org"], store_id, actor["id"], "mapping.activated",
                      {"version": mapping['id'], "fingerprint": mapping["fingerprint"]})
        return {"versions": versions, "state": "active"}

    @app.post(prefix + "/stores/{store_id}/mappings/{version}/activate")
    def activate(store_id: str, version: str, actor=Depends(user)):
        result = activate_mappings_atomic(store_id, [version], actor)
        return {"version": version, "state": "active"}

    @app.get(prefix + "/stores/{store_id}/capabilities")
    def capabilities(store_id: str, actor=Depends(user)):
        store = owned(store_id, actor)
        return {"capabilities": capability_view(
            mapping_rows(store_id), enabled_operations(store_id), source=connector_source(store)
        )}

    @app.put(prefix + "/stores/{store_id}/capabilities")
    def configure_capabilities(store_id: str, body: CapabilityControls, actor=Depends(user)):
        store = owned(store_id, actor)
        requested = {item.operation: item.enabled for item in body.capabilities}
        if set(requested) - set(BY_OPERATION):
            raise HTTPException(422, "Unknown canonical capability")
        active = active_operations(store_id)
        if any(enabled and operation not in active for operation, enabled in requested.items()):
            raise HTTPException(409, "A capability needs a tested active mapping before it can be enabled")
        now = time.time()
        with dbs.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for operation, enabled in requested.items():
                db.execute(
                    "INSERT INTO capability_controls(store,operation,enabled,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(store,operation) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated",
                    (store_id, operation, int(enabled), now),
                )
            db.execute("UPDATE stores SET verified=NULL,agent_access_enabled=0 WHERE id=?", (store_id,))
            db.execute("UPDATE routing_bindings SET verified=NULL WHERE store=?", (store_id,))
        dbs.event(actor["org"], store_id, actor["id"], "capabilities.updated", {"capabilities": requested})
        return {"capabilities": capability_view(
            mapping_rows(store_id), enabled_operations(store_id), source=connector_source(store)
        )}

    @app.get(prefix + "/stores/{store_id}/policy")
    def get_policy(store_id: str, actor=Depends(user)):
        return json.loads(owned(store_id, actor)["policy"])

    @app.put(prefix + "/stores/{store_id}/policy")
    async def put_policy(store_id: str, request: Request, actor=Depends(user)):
        owned(store_id, actor)
        try:
            policy = StorefrontPolicy.model_validate(await request.json())
        except Exception:
            raise HTTPException(422, "Invalid storefront policy configuration") from None
        with dbs.db() as db:
            db.execute(
                "UPDATE stores SET policy=?,policy_reviewed_at=?,agent_access_enabled=0 WHERE id=? AND org=?",
                (policy.model_dump_json(), time.time(), store_id, actor["org"]),
            )
        dbs.event(
            actor["org"], store_id, actor["id"], "policy.updated", {"fingerprint": digest(policy.model_dump_json())}
        )
        return policy

    def status_item(state, detail, evidence=None):
        return {"state": state, "detail": detail, **({"evidence": evidence} if evidence is not None else {})}

    def health_view(store, actor):
        exposed = active_operations(store["id"], exposed=True)
        hosted = hosted_connector_ready(store)
        from .installations import get_installation
        installation = get_installation(dbs, store["id"], store["environment"])
        native = bool(installation and installation["transport"] == "native_http" and not installation["revoked_at"])
        online = hosted or native or bool(store["heartbeat"] and store["heartbeat"] > time.time() - 40)
        verified = bool(store["verified"] and store["verified"] > time.time() - 86400)
        core = bool(exposed)
        try:
            StorefrontPolicy.model_validate_json(store["policy"])
            policy_ok = bool(store['policy_reviewed_at'])
        except Exception:
            policy_ok = False
        try:
            profile = discovery_document(store)
            profile_ok = bool(profile.get("ucp", {}).get("capabilities"))
        except HTTPException:
            profile_ok = False
        from .operational_health import operational_checks
        operational = operational_checks(app, dbs, store, exposed)
        shopify_ok = True
        if store['platform'] == 'shopify':
            try:
                app.state.require_shopify_installation(store['id'])
            except HTTPException:
                shopify_ok = False
        publication_required = store["environment"] == "production"
        discovery_ready = verified or not publication_required
        active = online and discovery_ready and core and policy_ok and profile_ok and shopify_ok and bool(store["agent_access_enabled"]) and all(item['state'] == 'active' for item in operational.values())
        if active:
            try:
                require_agent_access(store['id'])
            except HTTPException:
                active = False
        return {
            "protection": "active" if active else "needs_action",
            "checks": {
                "account": status_item("active", "Authenticated organization", actor["name"]),
                "store": status_item("active", "Store identity created", store["name"]),
                "domain": status_item(
                    "active" if verified else "needs_action" if publication_required else "waiting",
                    "Exact merchant-domain UCP profile verified" if verified else "Production requires exact merchant-domain publication" if publication_required else "Sandbox uses the managed Store-scoped profile; merchant-domain publication is not claimed",
                    store["verified"],
                ),
                "connector": status_item(
                    "active" if online else "waiting",
                    "Auteric hosted Shopify connector is ready"
                    if hosted
                    else "Signed Native HTTP runtime is registered and reachable"
                    if native
                    else "Merchant connector heartbeat is current"
                    if online
                    else "Waiting for a current merchant connector heartbeat",
                    {"mode": "auteric_hosted", "merchant_setup_required": False}
                    if hosted
                    else store["heartbeat"],
                ),
                **({'shopify_installation': status_item(
                    'active' if shopify_ok else 'needs_action',
                    'Shopify OAuth and Storefront catalog/cart credential are connected' if shopify_ok else 'Complete Shopify installation and Storefront provisioning',
                )} if store['platform'] == 'shopify' else {}),
                "commerce_capabilities": status_item(
                    "active" if core else "needs_action",
                    f"{len(exposed)} canonical capabilities enabled",
                    sorted(exposed),
                ),
                "ucp_profile": status_item(
                    "active" if profile_ok else "needs_action",
                    "Generated from enabled active mappings" if profile_ok else "HTTPS public URL and UCP capability set required",
                ),
                "gateway": status_item(
                    "active" if core else "waiting",
                    "Enabled actions route through the canonical gateway" if core else "Enable tested mappings first",
                ),
                "policies": status_item(
                    "active" if policy_ok else "failed",
                    "Business controls reviewed and valid" if policy_ok else "Review and save business controls before activation",
                ),
                "ucp_publication": status_item(
                    'active' if verified and profile_ok else 'needs_action' if publication_required else 'waiting',
                    'Exact merchant-domain publication is current' if verified else 'Required before production Agent Access' if publication_required else 'Optional in sandbox; direct managed-profile testing only',
                    store['verified'],
                ),
                "agent_access": status_item('active' if active else 'needs_action', 'Agent Access activated with current evidence' if active else 'Complete evidence requirements before activation'),
                **operational,
            },
            "agent_access_enabled": bool(store["agent_access_enabled"]),
            "test_transaction_available": core and online,
            "production_ready": False,
        }

    @app.get(prefix + "/stores/{store_id}/connection-health")
    def connection_health(store_id: str, actor=Depends(user)):
        store = owned(store_id, actor)
        return health_view(store, actor)

    @app.get(prefix + "/stores/{store_id}/agent-exposure")
    def agent_exposure(store_id: str, actor=Depends(user)):
        """Describe protected and bypass paths without collapsing them into one claim."""
        store = owned(store_id, actor)
        health = health_view(store, actor)
        enabled = active_operations(store_id, exposed=True)
        with dbs.db() as db:
            traffic_seen = db.execute(
                "SELECT count(*) FROM traffic WHERE store=?", (store_id,)
            ).fetchone()[0]
        shopify_native = store['platform'] == 'shopify'
        native_evidence = getattr(app.state, 'shopify_exposure', lambda _store: None)(store_id) if shopify_native else None
        protected_state = (
            'active' if health['protection'] == 'active'
            else 'ready' if enabled and health['checks']['connector']['state'] == 'active'
            else 'setup_required'
        )
        native = {
            'id': 'shopify_native' if shopify_native else 'existing_native',
            'name': 'Shopify Native Agent Commerce' if shopify_native else 'Existing Native Agent Commerce',
            'state': native_evidence['state'] if native_evidence else 'unknown',
            'managed_by': 'Shopify' if shopify_native else 'Merchant or platform',
            'auteric_enforcement': 'not_in_path' if shopify_native else 'unknown',
            'protected': False,
            'evidence': native_evidence['evidence'] if native_evidence else
                ('Shopify can provide native agent commerce, but this Store has not been checked yet'
                 if shopify_native else 'No repository or live endpoint exposure scan is attached to this Store'),
            'checked_at': native_evidence['checked'] if native_evidence else None,
            'http_status': native_evidence['http_status'] if native_evidence else None,
        }
        protected = {
            'id': 'auteric_protected',
            'name': 'Auteric Protected Agent Commerce',
            'state': protected_state,
            'managed_by': 'Auteric Gateway',
            'auteric_enforcement': 'in_path',
            'protected': True,
            'traffic_seen': traffic_seen > 0,
            'request_count': traffic_seen,
            'controls': {
                'policy_enforcement': True,
                'exact_approval': True,
                'state_validation': True,
                'audit': True,
            },
            'capabilities': sorted(enabled),
        }
        return {
            'store_id': store_id,
            'platform': store['platform'],
            # Whole-store protection requires separate evidence that no bypass
            # path exists. This endpoint currently proves only the managed path.
            'store_protected': False,
            'protected_path_active': protected_state == 'active',
            'known_unprotected_path': bool(shopify_native and native_evidence and native_evidence['state'] == 'detected'),
            'summary': 'Auteric protects only traffic sent to the Auteric managed endpoint.',
            'paths': [native, protected],
        }

    def activate_verified_agent_access(store_id: str, actor_id: str, *, automatic: bool = False):
        store = store_row(store_id)
        actor = {'id': actor_id, 'org': store['org'], 'name': 'Connection Test' if automatic else actor_id}
        if not store['agent_access_enabled']:
            app.state.require_gateway_running()
            if store['environment'] == 'production':
                app.state.require_production_configuration()
            if store['platform'] == 'shopify':
                app.state.require_shopify_installation(store_id)
            health = health_view(store, actor)
            required = ("connector", "commerce_capabilities", "ucp_profile", "gateway", "policies", "merchant_reachability", "policy_enforcement", "connection_test", "runtime_safety", "critical_errors")
            if store["environment"] == "production":
                required += ("domain", "ucp_publication")
            if store['platform'] == 'shopify':
                required += ('shopify_installation',)
            if any(health["checks"][name]["state"] != "active" for name in required):
                raise HTTPException(409, "Complete every verified Connection Health requirement before activation")
            with dbs.db() as db:
                db.execute("UPDATE stores SET agent_access_enabled=1 WHERE id=?", (store_id,))
            dbs.event(store["org"], store_id, actor_id, "agent_access.updated", {"enabled": True, "automatic": automatic})
        return {"enabled": True, "protection": "active", "automatic": automatic}

    app.state.activate_verified_agent_access = activate_verified_agent_access

    @app.put(prefix + "/stores/{store_id}/agent-access")
    async def configure_agent_access(store_id: str, body: AgentAccess, actor=Depends(user)):
        store = owned(store_id, actor)
        if body.enabled:
            return activate_verified_agent_access(store_id, actor['id'])
        with dbs.db() as db:
            db.execute("UPDATE stores SET agent_access_enabled=0 WHERE id=?", (store_id,))
        dbs.event(actor["org"], store_id, actor["id"], "agent_access.updated", {"enabled": False, "automatic": False})
        return {"enabled": False, "protection": "disabled", "automatic": False,
                "scanner_evidence": await app.state.invalidate_scanner_evidence(store)}

    @app.get(prefix + "/stores/{store_id}/protocols/webmcp")
    def webmcp_projection(store_id: str, actor=Depends(user)):
        owned(store_id, actor)
        enabled = active_operations(store_id, exposed=True)
        tools = []
        for capability in CAPABILITIES:
            if capability.operation not in enabled:
                continue
            tools.append(
                {
                    "name": capability.operation,
                    "title": capability.name,
                    "description": capability.description,
                    "input_schema": protocol_input_schema(INPUTS[capability.operation]),
                    "canonical_operation": capability.operation,
                    "risk": {
                        "side_effect": capability.side_effect,
                        "sensitivity": "public" if capability.side_effect == "read" else "sensitive",
                        "requires_approval": capability.side_effect != "read",
                    },
                }
            )
        return {
            "tools": tools,
            "source": "enabled_active_canonical_mappings",
            "gateway_path": "/api/auteric/execute",
            "contains_credentials": False,
        }

    @app.get(prefix + "/stores/{store_id}/traffic")
    def traffic(store_id: str, actor=Depends(user)):
        owned(store_id, actor)
        with dbs.db() as db:
            return [
                {
                    **dict(r),
                    **{k: json.loads(r[k]) if r[k] else None for k in ("input", "decision", "response", "error")},
                }
                for r in db.execute("SELECT * FROM traffic WHERE store=? ORDER BY created DESC LIMIT 100", (store_id,))
            ]

    @app.get(prefix + "/stores/{store_id}/audit")
    def audit(store_id: str, actor=Depends(user)):
        owned(store_id, actor)
        with dbs.db() as db:
            return [
                {**dict(r), "data": json.loads(r["data"])}
                for r in db.execute(
                    "SELECT * FROM audit WHERE store=? AND org=? ORDER BY id DESC LIMIT 200", (store_id, actor["org"])
                )
            ]

    def discovery_document(store):
        from .attestation import sign_exposure
        from .ucp import discovery

        active = list(active_operations(store["id"], exposed=True))
        with dbs.db() as db:
            configured = db.execute("SELECT * FROM ucp_config WHERE store=?", (store["id"],)).fetchone()
        ucp_configuration = {
            "payment_handlers": json.loads(configured["payment_handlers"]) if configured else {},
            "identity_linking": json.loads(configured["identity_linking"]) if configured and configured["identity_linking"] else None,
        }
        if not ucp_configuration["payment_handlers"] and "complete_checkout" in active:
            active.remove("complete_checkout")
        local_http = development and urlsplit(base).scheme == "http" and urlsplit(base).hostname in {"localhost", "127.0.0.1", "::1"}
        if urlsplit(base).scheme != "https" and not local_http:
            raise HTTPException(
                409,
                "Configure an HTTPS public URL before generating publishable UCP discovery; "
                "local canonical actions remain available",
            )
        if "get_product" in active:
            active.append("lookup_products")
        ucp_mcp_endpoint = app.state.mcp_public_url + "/ucp/" + store["id"] + "/mcp"
        profile = discovery(base + "/agent-commerce/" + store["domain"], store["domain"], active,
                            allow_local_http=local_http, mcp_endpoint=ucp_mcp_endpoint,
                            payment_handlers=ucp_configuration["payment_handlers"],
                            identity_linking=ucp_configuration["identity_linking"])
        # UCP's public service endpoint uses official UCP tool names.  The
        # Auteric extension advertises the canonical Gateway MCP transport
        # separately, so connectors never mix the two tool vocabularies.
        mcp_exposure = {"endpoint": app.state.mcp_public_url + "/mcp/" + store["id"],
                        "transport": "streamable-http", "protocol_version": "2025-11-25",
                        "ucp_version": "2026-08-25", "binding": "dev.ucp.shopping",
                        "authentication": "store_scoped_bearer",
                        "operations": sorted(op for op in active if op in BY_OPERATION)}
        profile["auteric_mcp"] = mcp_exposure
        profile["auteric_domain_verification"] = store["verification_token"]
        profile["auteric_attestation"] = sign_exposure(
            {
                "kind": "auteric.ucp.exposure.v1",
                "domain": store["domain"],
                "endpoint": base + "/agent-commerce/" + store["domain"],
                "store_id": store["id"],
                "capabilities": sorted(active),
                "mcp": mcp_exposure,
            },
            development=development,
        )
        if local_http:
            profile["auteric_local_test"] = True
        return profile

    def public_ucp_configuration(store_id):
        with dbs.db() as db:
            row = db.execute("SELECT payment_handlers,identity_linking,updated FROM ucp_config WHERE store=?", (store_id,)).fetchone()
        return {
            "payment_handlers": json.loads(row["payment_handlers"]) if row else {},
            "identity_linking": json.loads(row["identity_linking"]) if row and row["identity_linking"] else None,
            "updated": row["updated"] if row else None,
        }

    @app.get(prefix + "/stores/{store_id}/ucp-configuration")
    def get_ucp_configuration(store_id: str, actor=Depends(user)):
        owned(store_id, actor)
        return public_ucp_configuration(store_id)

    @app.put(prefix + "/stores/{store_id}/ucp-configuration")
    def set_ucp_configuration(store_id: str, body: UCPConfiguration, actor=Depends(user)):
        owned(store_id, actor)
        value = body.model_dump(mode="json")
        encoded = encode(value)
        if len(encoded) > 100_000:
            raise HTTPException(422, "UCP public configuration is too large")
        forbidden = re.compile(r"secret|password|private|access_token|refresh_token|card_number|cvv|cvc", re.I)
        def safe_public(item):
            if isinstance(item, dict):
                return all(not forbidden.search(str(key)) and safe_public(child) for key, child in item.items())
            if isinstance(item, list):
                return all(safe_public(child) for child in item)
            return True
        if not safe_public(value):
            raise HTTPException(422, "UCP discovery configuration must not contain credentials or payment secrets")
        now = time.time()
        with dbs.db() as db:
            db.execute(
                "INSERT INTO ucp_config VALUES(?,?,?,?) ON CONFLICT(store) DO UPDATE SET "
                "payment_handlers=excluded.payment_handlers,identity_linking=excluded.identity_linking,updated=excluded.updated",
                (store_id, encode(value["payment_handlers"]), encode(value["identity_linking"]) if value["identity_linking"] else None, now),
            )
        dbs.event(actor["org"], store_id, actor["id"], "ucp.configuration.updated", {
            "payment_handler_types": sorted(value["payment_handlers"]),
            "identity_linking": bool(value["identity_linking"]),
        })
        return public_ucp_configuration(store_id)

    @app.get(prefix + "/stores/{store_id}/discovery")
    def setup_discovery(store_id: str, actor=Depends(user)):
        store = owned(store_id, actor)
        shopify_native = store["platform"] == "shopify" and store["domain"].endswith(".myshopify.com")
        return {
            "document": discovery_document(store),
            "publish_url": None if shopify_native else "https://" + store["domain"] + "/.well-known/ucp",
            "platform_native_profile_url": "https://" + store["domain"] + "/.well-known/ucp" if shopify_native else None,
            "publication_control": "shopify_native" if shopify_native else "merchant_managed",
            "instructions": [
                "Shopify owns /.well-known/ucp on myshopify.com; Auteric does not overwrite or claim control of it.",
                "Use the managed Store-scoped profile below for sandbox Gateway acceptance.",
                "Production discovery through Auteric requires a merchant-controlled hostname or an exact edge route that publishes this profile.",
            ] if shopify_native else [
                "Publish this exact JSON only at /.well-known/ucp; leave the human storefront unchanged.",
                "Alternatively proxy only /.well-known/ucp to the managed profile URL shown below.",
                "Custom merchant subdomains need explicit TLS/custom-host provisioning; no ready DNS target is claimed.",
            ],
            # Bootstrap discovery must be fetchable before domain verification;
            # commerce execution remains guarded by the verified routing binding.
            "managed_profile_url": base + "/ucp/" + store["id"] + "/.well-known/ucp",
            "platform": store["platform"],
            "verified": bool(store["verified"]),
            "production_ready": False,
        }

    @app.post(prefix + "/stores/{store_id}/verify")
    async def verify(store_id: str, actor=Depends(user)):
        store = owned(store_id, actor)
        try:
            observed = await asyncio.wait_for(fetch_discovery(store["domain"]), 15)
            expected = discovery_document(store)
            if observed != expected:
                raise ValueError(
                    "Published discovery differs from the exact generated profile; publish current configuration"
                )
        except Exception as exc:
            logger.warning(
                "Public discovery verification failed for store=%s domain=%s reason=%s",
                store_id,
                store["domain"],
                type(exc).__name__,
            )
            with dbs.db() as db:
                db.execute("UPDATE stores SET verified=NULL,agent_access_enabled=0 WHERE id=?", (store_id,))
                db.execute("UPDATE routing_bindings SET verified=NULL WHERE store=?", (store_id,))
            raise HTTPException(
                422,
                f"Public HTTPS discovery did not match ({type(exc).__name__}). Check DNS/TLS, "
                "publish the exact current JSON at /.well-known/ucp, then retry.",
            ) from None
        with dbs.db() as db:
            checked_at = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE stores SET verified=?,verification_method='ucp_exact_document' WHERE id=?",
                (checked_at, store_id),
            )
            db.execute(
                "INSERT INTO routing_bindings(hostname,store,method,verified,created) VALUES(?,?,?,?,?) "
                "ON CONFLICT(hostname) DO UPDATE SET store=excluded.store,method=excluded.method,verified=excluded.verified",
                (store["domain"], store_id, "ucp_exact_document", checked_at, checked_at),
            )
        dbs.event(actor["org"], store_id, actor["id"], "ucp.discovery.verified")
        return {"verified": True, "checked_at": checked_at, "method": "ucp_exact_document", "production_ready": False}

    @app.post(prefix + "/stores/{store_id}/verify-local")
    async def verify_local(store_id: str, body: LocalVerification, actor=Depends(user)):
        """Check local publication only; never create a verified public routing binding."""
        if not development:
            raise HTTPException(404, "Local verification is unavailable")
        owned(store_id, actor)
        url = urlsplit(body.store_url)
        if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
            raise HTTPException(422, "Local store URL must be a plain HTTP loopback origin")
        target = body.store_url.rstrip("/") + "/.well-known/ucp"
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
                response = await client.get(target)
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                raise ValueError("Local UCP route is not available")
            observed = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(422, "Local UCP route is unavailable or invalid") from None
        expected = discovery_document(owned(store_id, actor))
        if observed != expected:
            raise HTTPException(422, "Local UCP document differs from the current service-issued document")
        return {"local_verified": True, "public_domain_verified": False,
                "runtime_protection_verified": False, "url": target}

    @app.get("/ucp/{store_id}/.well-known/ucp")
    def public_discovery(store_id: str):
        store = store_row(store_id)
        dbs.event(store["org"], store_id, "public", "ucp.discovery.requested")
        return discovery_document(store)

    def resolve_public_store(hostname):
        try:
            hostname = domain_name(hostname)
        except ValueError:
            raise HTTPException(404, "Unknown merchant endpoint") from None
        with dbs.db() as db:
            row = db.execute(
                "SELECT stores.* FROM routing_bindings JOIN stores ON stores.id=routing_bindings.store "
                "WHERE routing_bindings.hostname=? AND routing_bindings.verified>? AND stores.verified>? "
                "AND stores.domain=routing_bindings.hostname",
                (hostname, time.time() - 86400, time.time() - 86400),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Merchant endpoint is not verified")
        return dict(row)

    @app.get("/agent-commerce/{hostname}/.well-known/ucp")
    def managed_public_discovery(hostname: str):
        store = resolve_public_store(hostname)
        dbs.event(store["org"], store["id"], "public", "ucp.managed.discovery.requested")
        return discovery_document(store)

    app.state.dispatch = dispatch
    app.state.owned = owned
    app.state.store_row = store_row
    app.state.selected_mapping = selected_mapping
    app.state.auth_token = auth_token
    app.state.user_dependency = user
    app.state.create_session = create_session
    app.state.set_session_cookie = set_session_cookie
    app.state.runtime_class = StorefrontRuntime
    app.state.policy_class = StorefrontPolicy
    app.state.evaluate_policy = evaluate_policy
    app.state.validate_response = validate_response
    app.state.inputs = INPUTS
    app.state.operation_enabled = operation_enabled
    app.state.require_agent_access = require_agent_access
    app.state.credential_vault = vault
    app.state.resolve_public_store = resolve_public_store
    app.state.ucp_configuration = public_ucp_configuration
    from .operations import attach_operations

    attach_operations(app, dbs)
    from .transports.native_http import NativeHttpTransport, gateway_signing_keys

    signing_keys = gateway_signing_keys(development)
    app.state.native_transport = (
        NativeHttpTransport(signing_keys, dbs=dbs, issuer=base) if signing_keys else None
    )
    from .installations import attach_installations

    attach_installations(app, dbs)
    from .capabilities import attach_capability_controls, capability_enabled
    from .principals import attach_principals

    attach_capability_controls(app, dbs)
    attach_principals(app, dbs)

    def capability_controls_allowed(store_id, operation):
        """Effective-policy gate for display surfaces (e.g. MCP tools/list).

        Native-installation stores are governed by the server-side effective
        policy; worker-based stores keep the mapping-based capability controls.
        Per-call enforcement happens in dispatch regardless of this hook.
        """
        store = store_row(store_id)
        from .installations import get_installation

        installation = get_installation(dbs, store_id, store["environment"])
        if installation and installation["transport"] == "native_http":
            return capability_enabled(dbs, store, operation)
        return True

    app.state.capability_controls_allowed = capability_controls_allowed

    async def test_mapping_for_platform(store_id, version, input_data, actor):
        store = owned(store_id, actor)
        mode = 'production_read_only' if store['environment'] == 'production' else store['environment']
        return await contract_test(
            store_id, version, ContractTest(input=input_data, mode=mode), actor
        )

    def activate_mapping_for_platform(store_id, version, actor):
        return activate(store_id, version, actor)

    app.state.test_mapping_for_platform = test_mapping_for_platform
    app.state.activate_mapping_for_platform = activate_mapping_for_platform
    app.state.activate_mappings_atomic = activate_mappings_atomic
    from .scanner_evidence import install_scanner_evidence

    install_scanner_evidence(app, dbs)
    from .promotion import attach_promotion

    attach_promotion(app, dbs, mapping_rows)
    from .gateway import attach_gateway

    attach_gateway(app, dbs, base, development)
    from .sidecar_ingress import attach_sidecar_ingress
    attach_sidecar_ingress(app, dbs)
    from .direct_verification import install_direct_verification
    install_direct_verification(app, dbs)
    from .mcp import attach_mcp

    attach_mcp(app, dbs)
    from .ucp_mcp import attach_ucp_mcp

    attach_ucp_mcp(app, dbs, development=development)
    from .installation_oauth import attach_installation_oauth

    attach_installation_oauth(app, dbs, installation_mcp_base, development)
    from .installation_mcp import attach_installation_mcp

    attach_installation_mcp(app, dbs)
    from .shopify_installation import attach_shopify

    attach_shopify(app, dbs)

    web = ROOT / "apps/web/dist"
    if web.exists():
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

        # The authorization page is server-rendered while the console is a
        # bundled SPA. Serve the same public brand assets at stable paths so
        # both surfaces render the Scanner-identical identity in production.
        @app.get("/auteric-mark.png", include_in_schema=False)
        @app.get("/auteric-wordmark.png", include_in_schema=False)
        def brand_asset(request: Request):
            return FileResponse(web / request.url.path.lstrip("/"))

        @app.get("/console")
        @app.get("/connect")
        @app.get("/console/{rest:path}")
        @app.get("/")
        def frontend(rest: str = ""):
            return FileResponse(web / "index.html")

    return app


def _load_local_dotenv():
    """Load a local development .env without evaluating shell syntax.

    Deployment environments provide variables directly. This deliberately accepts
    only simple KEY=VALUE records and never overrides an explicitly supplied
    environment value.
    """
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            continue
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def factory():
    development = os.getenv("AUTERIC_COMMERCE_DEV") == "true"
    if development:
        _load_local_dotenv()
    return create_app(development=development)
