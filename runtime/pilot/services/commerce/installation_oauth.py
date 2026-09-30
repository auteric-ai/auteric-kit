"""OAuth 2.1-style authorization and device grants for Installation MCP."""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .storage import digest, uid
from .security import check_password

SCOPE = "installation.mcp"

async def fields(request):
    values = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    return {name: items[-1] for name, items in values.items()}

class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_type: str
    client_id: str = Field(min_length=1, max_length=120)
    code: str | None = None
    redirect_uri: str | None = None
    code_verifier: str | None = None
    device_code: str | None = None
    refresh_token: str | None = None

class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(min_length=1, max_length=120)
    scope: str = SCOPE

def attach_installation_oauth(app, dbs, base, development):
    with dbs.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS installation_oauth(
          id TEXT PRIMARY KEY,kind TEXT,client_id TEXT,redirect_uri TEXT,challenge TEXT,scope TEXT,state TEXT,user_id TEXT,
          code_hash TEXT,device_hash TEXT,user_code_hash TEXT,refresh_hash TEXT,expires REAL,consumed REAL,created REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS installation_oauth_tokens(
          access_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,scope TEXT NOT NULL,refresh_hash TEXT UNIQUE,expires REAL NOT NULL,revoked REAL,created REAL NOT NULL)""")

    def clients():
        raw = os.getenv("AUTERIC_INSTALLATION_MCP_CLIENTS")
        if raw:
            try: value = json.loads(raw)
            except ValueError: raise RuntimeError("AUTERIC_INSTALLATION_MCP_CLIENTS must be JSON")
            if not isinstance(value, dict): raise RuntimeError("OAuth clients must be an object")
            return value
        return {"auteric-installation-local": {"redirect_uris": ["http://127.0.0.1/callback", "http://localhost/callback"]}} if development else {}

    def client(client_id, redirect_uri=None):
        row = clients().get(client_id)
        if not isinstance(row, dict): raise HTTPException(400, "unknown_client")
        allowed = row.get("redirect_uris", [])
        if redirect_uri is not None and redirect_uri not in allowed: raise HTTPException(400, "invalid_redirect_uri")
        return row

    def pkce(verifier):
        if not verifier or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier): raise HTTPException(400, "invalid_grant")
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    def issue(user_id, scope):
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
        with dbs.db() as db: db.execute("INSERT INTO installation_oauth_tokens VALUES(?,?,?,?,?,?,?)", (digest(access), user_id, scope, digest(refresh), time.time()+3600, None, time.time()))
        return {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh, "scope": scope}

    async def request_model(request, model):
        if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
            try:
                values = await request.json()
            except ValueError:
                raise HTTPException(400, "invalid_request") from None
        else:
            values = await fields(request)
        try:
            return model.model_validate(values)
        except ValueError:
            raise HTTPException(400, "invalid_request") from None

    @app.get("/.well-known/oauth-authorization-server")
    def metadata():
        return {"issuer": base, "authorization_endpoint": base+"/oauth/authorize", "token_endpoint": base+"/oauth/token", "device_authorization_endpoint": base+"/oauth/device/code", "revocation_endpoint": base+"/oauth/revoke", "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "urn:ietf:params:oauth:grant-type:device_code", "refresh_token"], "code_challenge_methods_supported": ["S256"], "scopes_supported": [SCOPE]}

    @app.get("/.well-known/oauth-protected-resource")
    def protected_resource_metadata():
        return {"resource": base+"/installation-mcp", "authorization_servers": [base], "scopes_supported": [SCOPE]}

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    def authorize(response_type: str, client_id: str, redirect_uri: str, code_challenge: str, code_challenge_method: str, state: str):
        if response_type != "code" or code_challenge_method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) or not state: raise HTTPException(400, "invalid_request")
        client(client_id, redirect_uri)
        request_id = uid(); expires=time.time()+600
        with dbs.db() as db: db.execute("INSERT INTO installation_oauth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request_id,"authorization_code",client_id,redirect_uri,code_challenge,SCOPE,state,None,None,None,None,None,expires,None,time.time()))
        return HTMLResponse(f'''<!doctype html><title>Authorize Auteric Installation</title><form method="post" action="/oauth/authorize/approve"><input type="hidden" name="request_id" value="{request_id}"><label>Email <input name="email" type="email" required></label><label>Password <input name="password" type="password" required></label><button>Authorize Installation MCP</button></form>''')

    @app.post("/oauth/authorize/approve")
    async def approve(request: Request):
        form=await fields(request); request_id=str(form.get("request_id", "")); email=str(form.get("email", "")); password=str(form.get("password", ""))
        with dbs.db() as db:
            row=db.execute("SELECT * FROM installation_oauth WHERE id=? AND kind='authorization_code' AND user_id IS NULL AND expires>?", (request_id,time.time())).fetchone()
            user=db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not row or not user or not check_password(password, user["password"]): raise HTTPException(401,"authorization_denied")
            code=secrets.token_urlsafe(32); db.execute("UPDATE installation_oauth SET user_id=?,code_hash=? WHERE id=?", (user["id"],digest(code),request_id))
        return RedirectResponse(row["redirect_uri"]+("&" if "?" in row["redirect_uri"] else "?")+urlencode({"code":code,"state":row["state"]}), status_code=303)

    @app.post("/oauth/device/code")
    async def device(request: Request):
        body = await request_model(request, DeviceRequest)
        client(body.client_id)
        if body.scope != SCOPE: raise HTTPException(400,"invalid_scope")
        code=secrets.token_urlsafe(32); user_code="-".join((str(secrets.randbelow(10**4)).zfill(4) for _ in range(2)))
        with dbs.db() as db: db.execute("INSERT INTO installation_oauth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (uid(),"device",body.client_id,None,None,SCOPE,None,None,None,digest(code),digest(user_code),None,time.time()+600,None,time.time()))
        return {"device_code":code,"user_code":user_code,"verification_uri":base+"/oauth/device","verification_uri_complete":base+"/oauth/device?user_code="+user_code,"expires_in":600,"interval":5}

    @app.get("/oauth/device", response_class=HTMLResponse)
    def device_page(user_code: str = ""):
        return HTMLResponse(f'''<!doctype html><title>Authorize device</title><form method="post" action="/oauth/device/approve"><label>Code <input name="user_code" value="{user_code}" required></label><label>Email <input name="email" type="email" required></label><label>Password <input name="password" type="password" required></label><button>Authorize</button></form>''')

    @app.post("/oauth/device/approve")
    async def device_approve(request: Request):
        form=await fields(request); code=str(form.get("user_code", "")); email=str(form.get("email", "")); password=str(form.get("password", ""))
        with dbs.db() as db:
            row=db.execute("SELECT * FROM installation_oauth WHERE kind='device' AND user_code_hash=? AND user_id IS NULL AND expires>?",(digest(code),time.time())).fetchone(); user=db.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
            if not row or not user or not check_password(password,user["password"]): raise HTTPException(401,"authorization_denied")
            db.execute("UPDATE installation_oauth SET user_id=? WHERE id=?",(user["id"],row["id"]))
        return HTMLResponse("<title>Authorized</title>Device authorized. Return to your coding agent.")

    @app.post("/oauth/token")
    async def token(request: Request):
        body = await request_model(request, TokenRequest)
        client(body.client_id)
        user_id = scope = None
        with dbs.db() as db:
            if body.grant_type=="authorization_code":
                row=db.execute("SELECT * FROM installation_oauth WHERE code_hash=? AND client_id=? AND expires>? AND consumed IS NULL",(digest(body.code or ""),body.client_id,time.time())).fetchone()
                if not row or body.redirect_uri!=row["redirect_uri"] or not secrets.compare_digest(row["challenge"],pkce(body.code_verifier)): raise HTTPException(400,"invalid_grant")
                db.execute("UPDATE installation_oauth SET consumed=? WHERE id=?",(time.time(),row["id"])); user_id, scope = row["user_id"], row["scope"]
            elif body.grant_type=="urn:ietf:params:oauth:grant-type:device_code":
                row=db.execute("SELECT * FROM installation_oauth WHERE device_hash=? AND client_id=? AND expires>? AND consumed IS NULL",(digest(body.device_code or ""),body.client_id,time.time())).fetchone()
                if not row: raise HTTPException(400,"invalid_grant")
                if not row["user_id"]: raise HTTPException(400,"authorization_pending")
                db.execute("UPDATE installation_oauth SET consumed=? WHERE id=?",(time.time(),row["id"])); user_id, scope = row["user_id"], row["scope"]
            elif body.grant_type=="refresh_token":
                row=db.execute("SELECT * FROM installation_oauth_tokens WHERE refresh_hash=? AND revoked IS NULL",(digest(body.refresh_token or ""),)).fetchone()
                if not row: raise HTTPException(400,"invalid_grant")
                db.execute("UPDATE installation_oauth_tokens SET revoked=? WHERE access_hash=?",(time.time(),row["access_hash"])); user_id, scope = row["user_id"], row["scope"]
            else:
                raise HTTPException(400,"unsupported_grant_type")
        return issue(user_id, scope)

    @app.post("/oauth/revoke")
    async def revoke(request: Request):
        form=await fields(request); token=str(form.get("token", ""))
        with dbs.db() as db: db.execute("UPDATE installation_oauth_tokens SET revoked=? WHERE (access_hash=? OR refresh_hash=?) AND revoked IS NULL",(time.time(),digest(token),digest(token)))
        return {}
