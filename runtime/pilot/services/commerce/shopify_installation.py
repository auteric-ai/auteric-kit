"""Standalone Shopify OAuth and installation lifecycle, scoped to an owned Store.

Admin credentials remain encrypted on the server. A connector can retrieve only
its installed Storefront credential. No callback enables commerce capabilities.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from auteric_edge.http import request_json
from auteric_edge.manual import ManualConnector
from auteric_edge.platforms.shopify import ShopifyConnector
from .adapter_manifest import activation_requirements, manifest_for
from .storage import digest, encode
from .storage import uid
from .security import password_hash

# Storefront cart reads are authorized by the cart capability granted through
# unauthenticated_write_checkouts. Shopify's OAuth token response does not
# return unauthenticated_read_checkouts, even when the Admin UI displays it.
# Requiring a scope that Shopify never grants makes a valid installation fail.
SCOPES = frozenset({'read_products', 'unauthenticated_read_product_listings', 'unauthenticated_write_checkouts'})
VERSION = '2026-07'
OPERATIONS = ('search_products', 'get_product', 'create_cart', 'get_cart', 'add_to_cart',
              'update_cart_item', 'remove_from_cart', 'create_checkout', 'get_checkout')
HOSTED_CONNECTOR_VERSION = '0.1.0-shopify-hosted'
HOSTED_CONNECTOR_RELEASE = hashlib.sha256(b'auteric-shopify-hosted-2026-07-v1').hexdigest()
SHOPIFY_APP_CONFIG = Path(__file__).resolve().parents[2] / 'apps/shopify/shopify.app.toml'
SHOPIFY_APP_CONFIG_DIGEST = hashlib.sha256(SHOPIFY_APP_CONFIG.read_bytes()).hexdigest()


class InstallInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shop: str


class LiveAcceptanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: str


class PreparationRecoveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    acknowledge_abandoned_test_cart: bool


def shop_domain(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com', value):
        raise HTTPException(422, 'Use the exact lowercase myshopify.com domain')
    return value


def callback_hmac(pairs, secret):
    if len(dict(pairs)) != len(pairs):
        return False
    params = dict(pairs)
    signature = params.pop('hmac', '')
    message = '&'.join(f'{key}={value}' for key, value in sorted(params.items()))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(re.fullmatch('[a-f0-9]{64}', signature)) and hmac.compare_digest(expected, signature)


def attach_shopify(app, dbs):
    with dbs.db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS shopify_oauth(state TEXT PRIMARY KEY,store TEXT,actor TEXT,
          shop TEXT,expires REAL,consumed REAL);
        CREATE TABLE IF NOT EXISTS shopify_installations(store TEXT PRIMARY KEY,shop TEXT UNIQUE,
          status TEXT,secrets TEXT,scopes TEXT,currency TEXT,installed REAL,updated REAL);
        CREATE TABLE IF NOT EXISTS shopify_events(event TEXT PRIMARY KEY,shop TEXT,topic TEXT,
          body_hash TEXT,state TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS shopify_hook_registration(store TEXT,topic TEXT,uri TEXT,
          state TEXT,updated REAL,PRIMARY KEY(store,topic,uri));
        CREATE TABLE IF NOT EXISTS shopify_preparation(store TEXT PRIMARY KEY,
          state TEXT NOT NULL,result TEXT,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS shopify_exposure(store TEXT PRIMARY KEY,
          state TEXT NOT NULL,endpoint TEXT,http_status INTEGER,content_hash TEXT,
          evidence TEXT,checked REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS shopify_release_certifications(store TEXT,mapping TEXT,
          evidence TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(store,mapping));
        ''')

    def settings():
        # Standard Shopify local names remain supported for the developer
        # workflow; hosted environments use the explicit Auteric names.
        key = os.environ.get('AUTERIC_SHOPIFY_CLIENT_ID') or os.environ.get('SHOPIFY_CLIENT_ID', '')
        secret = os.environ.get('AUTERIC_SHOPIFY_CLIENT_SECRET') or os.environ.get('SHOPIFY_CLIENT_SECRET', '')
        encryption = os.environ.get('AUTERIC_SHOPIFY_ENCRYPTION_KEY', '')
        base = app.state.public_url
        if not key or not secret or not encryption or urlsplit(base).scheme != 'https':
            raise HTTPException(503, 'Configure Shopify app ID, secret, encryption key and public HTTPS origin')
        try:
            cipher = Fernet(encryption.encode())
        except ValueError:
            raise HTTPException(503, 'Invalid Shopify encryption key') from None
        return key, secret, cipher, base + '/api/commerce/shopify/callback'

    def hosted_release():
        value = os.environ.get('AUTERIC_SHOPIFY_RELEASE_DIGEST', '')
        if re.fullmatch('[a-f0-9]{64}', value):
            return value
        if not app.state.development:
            raise HTTPException(503, 'A deployment artifact digest is required for the hosted Shopify connector')
        return HOSTED_CONNECTOR_RELEASE

    def installation(store_id):
        with dbs.db() as db:
            row = db.execute('SELECT * FROM shopify_installations WHERE store=?', (store_id,)).fetchone()
        return dict(row) if row else None

    def decrypt(row):
        try:
            data = json.loads(settings()[2].decrypt(row['secrets'].encode()))
            if data['store'] != row['store'] or data['shop'] != row['shop']:
                raise ValueError('binding mismatch')
            return data
        except Exception:
            raise HTTPException(503, 'Installation secret is unavailable; reconnect the Store') from None

    def seal(store, shop, data):
        return settings()[2].encrypt(encode({**data, 'store': store, 'shop': shop}).encode()).decode()

    async def exchange(shop, body):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False,
                                         transport=getattr(app.state, 'shopify_transport', None)) as client:
                result = await request_json(client, 'POST', f'https://{shop}/admin/oauth/access_token', json=body)
            if not isinstance(result, dict) or not result.get('access_token'):
                raise ValueError('missing token')
            return result
        except Exception:
            raise HTTPException(502, 'Shopify token exchange failed; reconnect rather than replaying a consumed code') from None

    def token_data(result):
        # Expiring offline tokens only. Never assume a refresh response is complete.
        for name in ('expires_in', 'refresh_token_expires_in'):
            if not isinstance(result.get(name), int) or result[name] <= 0:
                raise HTTPException(502, 'Shopify did not return a valid expiring offline token')
        if not isinstance(result.get('refresh_token'), str) or not result['refresh_token']:
            raise HTTPException(502, 'Shopify did not return a refresh token')
        return {'access_token': result['access_token'], 'refresh_token': result['refresh_token'],
                'expires': time.time() + result['expires_in'],
                'refresh_expires': time.time() + result['refresh_token_expires_in']}

    async def admin_token(store_id, *, recovery=False):
        row = installation(store_id)
        allowed = {'installed', 'connected'} | ({'provisioning_unknown'} if recovery else set())
        if not row or row['status'] not in allowed:
            raise HTTPException(409, 'Install or reconnect Shopify first')
        data = decrypt(row)
        if data['expires'] > time.time() + 60:
            return row, data
        if data['refresh_expires'] <= time.time():
            raise HTTPException(409, 'Shopify authorization expired; reconnect')
        with dbs.db() as db:
            changed = db.execute("UPDATE shopify_installations SET status='refreshing' WHERE store=? AND status=? AND secrets=?", (store_id, row['status'], row['secrets'])).rowcount
        if not changed:
            raise HTTPException(409, 'Shopify credential refresh is already in progress')
        key, secret, _, _ = settings()
        try:
            result = await exchange(row['shop'], {'client_id': key, 'client_secret': secret,
                                    'grant_type': 'refresh_token', 'refresh_token': data['refresh_token']})
            data.update(token_data(result))
            with dbs.db() as db:
                # Uninstall during refresh must win over the returned token.
                changed = db.execute("UPDATE shopify_installations SET secrets=?,status=?,updated=? WHERE store=? AND status='refreshing' AND secrets=?", (seal(store_id, row['shop'], data), row['status'], time.time(), store_id, row['secrets'])).rowcount
            if not changed:
                raise HTTPException(409, 'Shopify installation changed during token refresh')
        except Exception:
            with dbs.db() as db:
                db.execute("UPDATE shopify_installations SET status='reconnect_required' WHERE store=? AND status='refreshing'", (store_id,))
            raise
        return installation(store_id), data

    async def admin_query(row, data, query, variables=None):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False,
                                         transport=getattr(app.state, 'shopify_transport', None)) as client:
                result = await request_json(client, 'POST', f'https://{row["shop"]}/admin/api/{VERSION}/graphql.json',
                    headers={'X-Shopify-Access-Token': data['access_token']},
                    json={'query': query, 'variables': variables or {}})
            if not isinstance(result, dict) or result.get('errors') or not isinstance(result.get('data'), dict):
                raise ValueError('invalid Admin response')
            return result['data']
        except Exception:
            raise HTTPException(502, 'Auteric could not verify the Shopify Admin response') from None

    def require_installation(store_id):
        row = installation(store_id)
        if not row or row['status'] != 'connected' or not SCOPES <= set(json.loads(row['scopes'])):
            raise HTTPException(409, 'A connected Shopify installation with required permissions is needed')
    app.state.require_shopify_installation = require_installation

    def hosted_ready(store_id):
        try:
            require_installation(store_id)
            row = installation(store_id)
            data = decrypt(row)
            store = app.state.store_row(store_id)
            return bool(data.get('storefront_token') and row.get('currency') and
                        store.get('connector_release') == hosted_release())
        except HTTPException:
            return False

    class ControlPlaneShopifyState:
        """Opaque Shopify cart aliases stored with the tenant control plane."""

        def __init__(self, store_id, shop):
            self.store_id = store_id
            self.shop = shop

        def remember(self, remote):
            if not isinstance(remote, str) or not remote.startswith('gid://shopify/Cart/'):
                raise ValueError('Invalid Shopify cart identity')
            with dbs.db() as db:
                row = db.execute(
                    'SELECT alias FROM shopify_cart_aliases WHERE store=? AND shop=? AND remote=?',
                    (self.store_id, self.shop, remote),
                ).fetchone()
                if row:
                    return row[0]
                alias = 'cart_' + uid()
                db.execute(
                    'INSERT INTO shopify_cart_aliases(store,shop,alias,remote,created) VALUES(?,?,?,?,?)',
                    (self.store_id, self.shop, alias, remote, time.time()),
                )
                return alias

        def resolve(self, alias):
            if not isinstance(alias, str) or not alias.startswith('cart_'):
                raise ValueError('Unknown Shopify cart')
            with dbs.db() as db:
                row = db.execute(
                    'SELECT remote FROM shopify_cart_aliases WHERE store=? AND shop=? AND alias=?',
                    (self.store_id, self.shop, alias),
                ).fetchone()
            if row is None:
                raise ValueError('Unknown Shopify cart; its durable state is unavailable')
            return row[0]

    app.state.shopify_cart_state = lambda store_id, shop: ControlPlaneShopifyState(store_id, shop)

    async def hosted_execute(store_id, operation, request, mapping):
        """Execute a reviewed Shopify mapping inside Auteric infrastructure.

        Merchant credentials never enter a browser or job payload. A connector is
        opened for one bounded operation so failed/uncertain writes retain the
        existing durable job and reconciliation semantics.
        """
        require_installation(store_id)
        row = installation(store_id)
        data = decrypt(row)
        connector = ManualConnector(ShopifyConnector(
            row['shop'], data['storefront_token'],
            database='unused-with-control-plane-state',
            state=ControlPlaneShopifyState(store_id, row['shop']),
            currency=row['currency'],
            transport=getattr(app.state, 'shopify_storefront_transport', None),
        ))
        try:
            return await connector.execute(operation, request, mapping)
        finally:
            await connector.close()

    app.state.shopify_hosted_ready = hosted_ready
    app.state.shopify_hosted_execute = hosted_execute

    def exposure_record(store_id):
        with dbs.db() as db:
            row = db.execute('SELECT * FROM shopify_exposure WHERE store=?', (store_id,)).fetchone()
        return dict(row) if row else None

    async def scan_exposure(store_id):
        row = installation(store_id)
        if not row or row['status'] not in {'installed', 'connected'}:
            raise HTTPException(409, 'Install Shopify before checking native exposure')
        endpoint = f'https://{row["shop"]}/.well-known/ucp'
        state, status, content_hash, evidence = 'not_detected', None, None, 'No valid public Shopify UCP document was observed'
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False,
                    transport=getattr(app.state, 'shopify_exposure_transport', None)) as client:
                response = await client.get(endpoint, headers={'Accept': 'application/json'})
            status = response.status_code
            raw = response.content
            if len(raw) > 1_000_000:
                raise ValueError('response too large')
            content_hash = hashlib.sha256(raw).hexdigest()
            content_type = response.headers.get('content-type', '').lower()
            payload = response.json() if response.status_code == 200 and 'json' in content_type else None
            if isinstance(payload, dict):
                state, evidence = 'detected', 'A public JSON document was observed at Shopify’s native UCP endpoint'
            elif response.status_code in {401, 403}:
                state, evidence = 'restricted', 'The native endpoint is access-restricted; exposure could not be determined'
        except Exception:
            state, evidence = 'unavailable', 'The native endpoint could not be checked; no exposure conclusion was made'
        checked = time.time()
        with dbs.db() as db:
            db.execute('INSERT INTO shopify_exposure VALUES(?,?,?,?,?,?,?) ON CONFLICT(store) DO UPDATE SET '
                       'state=excluded.state,endpoint=excluded.endpoint,http_status=excluded.http_status,'
                       'content_hash=excluded.content_hash,evidence=excluded.evidence,checked=excluded.checked',
                       (store_id, state, endpoint, status, content_hash, evidence, checked))
        return exposure_record(store_id)

    app.state.shopify_exposure = exposure_record
    app.state.shopify_scan_exposure = scan_exposure

    def validate_hosted_certification(store, mapping, evidence):
        if store['platform'] != 'shopify' or not hosted_ready(store['id']):
            raise HTTPException(409, 'Hosted Shopify connector is not ready')
        if json.loads(mapping['body']) != {'kind': 'sdk', 'operation': mapping['operation']}:
            raise HTTPException(409, 'Certification applies only to the built-in Shopify mapping')
        with dbs.db() as db:
            row = db.execute('SELECT evidence FROM shopify_release_certifications WHERE store=? AND mapping=?',
                             (store['id'], mapping['id'])).fetchone()
        if not row or json.loads(row['evidence']) != evidence or evidence.get('connector_release') != hosted_release():
            raise HTTPException(409, 'Hosted Shopify certification is missing or belongs to another release')

    app.state.validate_hosted_certification = validate_hosted_certification

    def draft_native_mappings(store_id, actor):
        with dbs.db() as db:
            existing = {row['operation'] for row in db.execute("SELECT operation FROM mappings WHERE store=? AND state IN ('draft','active')", (store_id,))}
        created = []
        for operation in OPERATIONS:
            if operation not in existing:
                created.append(app.state.save_mapping(store_id, operation, {'kind': 'sdk'}, actor)['id'])
        return created

    root = '/api/commerce/stores/{store_id}/shopify'

    def authorization(shop, store_id='', actor_id=''):
        key, _, _, callback = settings()
        state = secrets.token_urlsafe(32)
        with dbs.db() as db:
            db.execute('INSERT INTO shopify_oauth VALUES(?,?,?,?,?,NULL)',
                       (digest(state), store_id, actor_id, shop, time.time() + 600))
        url = f'https://{shop}/admin/oauth/authorize?' + urlencode({
            'client_id': key, 'scope': ','.join(sorted(SCOPES)),
            'redirect_uri': callback, 'state': state})
        return state, url

    def oauth_cookie(response, state):
        response.set_cookie('auteric_shopify_oauth', state, httponly=True,
                            secure=not app.state.development, samesite='lax', max_age=600,
                            path='/api/commerce/shopify/callback')

    @app.get('/api/commerce/shopify/install')
    def shopify_first_install(shop: str, request: Request):
        shop = shop_domain(shop)
        if not dbs.limit('shopify-install:' + (request.client.host if request.client else 'unknown'), 20, 600):
            raise HTTPException(429, 'Too many installation attempts; try later')
        state, url = authorization(shop)
        response = RedirectResponse(url, status_code=303)
        oauth_cookie(response, state)
        return response

    @app.get(root)
    def status(store_id: str, actor=Depends(app.state.user_dependency)):
        store = app.state.owned(store_id, actor)
        row = installation(store_id)
        with dbs.db() as db:
            active_count = db.execute(
                "SELECT count(DISTINCT operation) FROM mappings WHERE store=? AND state='active'",
                (store_id,),
            ).fetchone()[0]
            preparation = db.execute('SELECT state,updated FROM shopify_preparation WHERE store=?', (store_id,)).fetchone()
        try:
            _, _, _, callback = settings()
            configured = True
        except HTTPException:
            configured, callback = False, None
        # The reviewed hosted surface: production activates the live-read-verified
        # core set; sandbox/staging preparation covers the full reviewed registry
        # minus the two atomic-cart operations Shopify cannot perform honestly.
        reviewed = len(OPERATIONS) if store['environment'] == 'production' else len(app.state.inputs) - 2
        return {'configured': configured, 'callback_url': callback, 'required_scopes': sorted(SCOPES),
                'connector_mode': 'auteric_hosted', 'merchant_worker_required': False,
                'capabilities_prepared': active_count == reviewed,
                'preparation': dict(preparation) if preparation else None,
                'protected_capability_count': active_count,
                'installation': {k: row[k] for k in ('shop', 'status', 'scopes', 'currency', 'installed', 'updated')} if row else None}

    @app.get(root + '/production-preflight')
    def production_preflight(store_id: str, actor=Depends(app.state.user_dependency)):
        store = app.state.owned(store_id, actor)
        try:
            settings()
            configured = True
        except HTTPException:
            configured = False
        row = installation(store_id)
        exposure = exposure_record(store_id)
        with dbs.db() as db:
            active = db.execute("SELECT count(DISTINCT operation) FROM mappings WHERE store=? AND state='active'", (store_id,)).fetchone()[0]
            preparation = db.execute('SELECT state,result FROM shopify_preparation WHERE store=?', (store_id,)).fetchone()
            hooks = {item['topic']: item['state'] for item in db.execute(
                'SELECT topic,state FROM shopify_hook_registration WHERE store=?', (store_id,))}
        prepared = bool(preparation and preparation['state'] == 'passed')
        prepared_release = json.loads(preparation['result']).get('connector_release') if prepared else None
        checks = {
            'app_configuration': {'ready': configured, 'detail': 'Shopify credentials and HTTPS callback configuration are valid' if configured else 'missing or invalid'},
            'installation': {'ready': bool(row and row['status'] == 'connected'), 'detail': row['status'] if row else 'not_installed'},
            'hosted_connector': {'ready': hosted_ready(store_id), 'detail': 'Auteric-hosted; no merchant Worker required'},
            'capability_evidence': {'ready': active == len(OPERATIONS) and prepared and prepared_release == store['connector_release'],
                                    'detail': f'{active}/{len(OPERATIONS)} mappings active for current release'},
            'native_exposure_evidence': {'ready': bool(exposure and exposure['checked'] > time.time() - 86400),
                                         'detail': exposure['state'] if exposure else 'not_checked'},
            'lifecycle_webhooks': {'ready': all(hooks.get(topic) == 'observed' for topic in ('APP_UNINSTALLED', 'APP_SCOPES_UPDATE')),
                                   'detail': hooks},
            'production_release_digest': {'ready': bool(re.fullmatch('[a-f0-9]{64}', os.environ.get('AUTERIC_SHOPIFY_RELEASE_DIGEST', ''))),
                                          'detail': 'deployment-provided digest required'},
            'shopify_app_config_release': {
                'ready': hmac.compare_digest(os.environ.get('AUTERIC_SHOPIFY_APP_CONFIG_DIGEST', ''),
                                             SHOPIFY_APP_CONFIG_DIGEST),
                'detail': 'released Shopify app configuration must match this artifact'},
            'distributed_durable_storage': {
                'ready': dbs.postgres,
                'detail': 'PostgreSQL durable control-plane storage' if dbs.postgres
                          else 'Current control-plane storage is single-node SQLite',
            },
            'production_environment': {'ready': store['environment'] == 'production' and not app.state.development,
                                       'detail': store['environment']},
        }
        ready = all(item['ready'] for item in checks.values())
        return {'ready': ready, 'deployment_performed': False, 'checks': checks,
                'blockers': [name for name, item in checks.items() if not item['ready']]}

    @app.post(root + '/exposure/scan')
    async def refresh_exposure(store_id: str, actor=Depends(app.state.user_dependency)):
        app.state.owned(store_id, actor)
        result = await scan_exposure(store_id)
        dbs.event(actor['org'], store_id, actor['id'], 'shopify.native_exposure.checked',
                  {k: result[k] for k in ('state', 'endpoint', 'http_status', 'content_hash', 'checked')})
        return result

    @app.post(root + '/install')
    def start(store_id: str, body: InstallInput, actor=Depends(app.state.user_dependency)):
        store = app.state.owned(store_id, actor)
        if store['platform'] != 'shopify':
            raise HTTPException(409, 'Select a Shopify Store')
        shop = shop_domain(body.shop)
        settings()
        with dbs.db() as db:
            bound = db.execute('SELECT shop FROM shopify_installations WHERE store=?', (store_id,)).fetchone()
            if bound and bound['shop'] != shop:
                raise HTTPException(409, 'Create a separate Store for a different Shopify shop')
            existing = db.execute('SELECT store FROM shopify_installations WHERE shop=?', (shop,)).fetchone()
            if existing and existing['store'] != store_id:
                raise HTTPException(409, 'This Shopify shop is already bound to another Store')
        state, url = authorization(shop, store_id, actor['id'])
        response = JSONResponse({'authorization_url': url})
        # The Console cookie is Strict. A separate short-lived Lax cookie binds
        # the top-level cross-site OAuth return without weakening Console CSRF.
        oauth_cookie(response, state)
        return response

    @app.get('/api/commerce/shopify/callback')
    async def callback(request: Request):
        key, secret, _, _ = settings()
        params = request.query_params
        if not callback_hmac(list(params.multi_items()), secret):
            raise HTTPException(401, 'Invalid Shopify callback signature')
        shop = shop_domain(params.get('shop'))
        try:
            if abs(time.time() - int(params.get('timestamp', '0'))) > 600:
                raise ValueError()
        except ValueError:
            raise HTTPException(401, 'Expired Shopify callback') from None
        if not secrets.compare_digest(params.get('state', ''), request.cookies.get('auteric_shopify_oauth', '')) or not params.get('state'):
            raise HTTPException(401, 'Shopify authorization belongs to another browser')
        state = digest(params.get('state', ''))
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM shopify_oauth WHERE state=? AND shop=? AND consumed IS NULL AND expires>?', (state, shop, time.time())).fetchone()
            if not row or not params.get('code'):
                raise HTTPException(401, 'Invalid or already consumed Shopify authorization')
            store_id = row['store']
            actor = None
            if store_id:
                actor_row = db.execute('SELECT * FROM users WHERE id=?', (row['actor'],)).fetchone()
                if not actor_row:
                    raise HTTPException(401, 'Shopify installation owner no longer exists')
                actor = dict(actor_row)
                app.state.owned(store_id, actor)
            db.execute('UPDATE shopify_oauth SET consumed=? WHERE state=?', (time.time(), state))
        result = await exchange(shop, {'client_id': key, 'client_secret': secret, 'code': params['code'], 'expiring': 1})
        granted = {scope.strip() for scope in result.get('scope', '').split(',') if scope.strip()}
        if not SCOPES <= granted:
            raise HTTPException(409, 'Shopify did not grant required scopes: ' + ', '.join(sorted(SCOPES - granted)))
        data = token_data(result)
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            other = db.execute('SELECT store FROM shopify_installations WHERE shop=?', (shop,)).fetchone()
            if not store_id and other:
                store_id = other['store']
                store_row = db.execute('SELECT * FROM stores WHERE id=?', (store_id,)).fetchone()
                actor_row = db.execute('SELECT * FROM users WHERE org=? ORDER BY id LIMIT 1', (store_row['org'],)).fetchone()
                if not actor_row:
                    raise HTTPException(409, 'Existing Shopify Store has no owner')
                actor = dict(actor_row)
            elif not store_id:
                actor = {'id': uid(), 'org': uid(), 'name': shop.split('.')[0],
                         'email': f'shopify+{digest(shop)[:20]}@auteric.invalid'}
                store_id = uid()
                db.execute('INSERT INTO users VALUES(?,?,?,?,?)',
                           (actor['id'], actor['email'], password_hash(secrets.token_urlsafe(48)), actor['org'], actor['name']))
                db.execute('INSERT INTO stores(id,org,name,domain,environment,verification_token,verified,heartbeat,connector_version,policy,created,platform,integration_mode,agent_access_enabled,verification_method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (store_id, actor['org'], actor['name'], shop, 'production', secrets.token_urlsafe(32),
                            None, None, None, app.state.policy_class().model_dump_json(), time.time(),
                            'shopify', 'protect_existing', 0, None))
            if other and other['store'] != store_id:
                raise HTTPException(409, 'Shopify shop was bound by another installation')
            db.execute('INSERT INTO shopify_installations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(store) DO UPDATE SET shop=excluded.shop,status=excluded.status,secrets=excluded.secrets,scopes=excluded.scopes,currency=NULL,installed=excluded.installed,updated=excluded.updated', (store_id, shop, 'installed', seal(store_id, shop, data), encode(sorted(granted)), None, time.time(), time.time()))
            db.execute('UPDATE stores SET agent_access_enabled=0 WHERE id=?', (store_id,))
        dbs.event(actor['org'], store_id, actor['id'], 'shopify.app_installed',
                  {'shop': shop, 'scopes': sorted(granted), 'agent_access_enabled': False})
        response = RedirectResponse('/console?store=' + store_id, status_code=303)
        if request.cookies.get('auteric_session'):
            with dbs.db() as db:
                db.execute('DELETE FROM sessions WHERE token=?',
                           (digest(request.cookies['auteric_session']),))
        app.state.set_session_cookie(response, app.state.create_session(actor))
        response.delete_cookie('auteric_shopify_oauth', path='/api/commerce/shopify/callback')
        return response

    @app.post(root + '/connect')
    async def connect(store_id: str, actor=Depends(app.state.user_dependency)):
        app.state.owned(store_id, actor)
        release = hosted_release()
        row, data = await admin_token(store_id)
        if data.get('storefront_token') and row['status'] == 'connected':
            with dbs.db() as db:
                db.execute(
                    'UPDATE stores SET connector_version=?,connector_release=? WHERE id=?',
                    (HOSTED_CONNECTOR_VERSION, release, store_id),
                )
            exposure = await scan_exposure(store_id)
            return {'connected': True, 'connector_mode': 'auteric_hosted',
                    'merchant_worker_required': False,
                    'native_exposure': {'state': exposure['state'], 'checked': exposure['checked']},
                    'mapping_drafts_created': draft_native_mappings(store_id, actor)}
        # Provision once. An uncertain mutation requires manual reconciliation.
        with dbs.db() as db:
            changed = db.execute("UPDATE shopify_installations SET status='provisioning' WHERE store=? AND status='installed' AND secrets=?", (store_id, row['secrets'])).rowcount
        if not changed:
            raise HTTPException(409, 'Storefront provisioning is already running or needs reconciliation')
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=getattr(app.state, 'shopify_transport', None)) as client:
                result = await request_json(client, 'POST', f'https://{row["shop"]}/admin/api/{VERSION}/graphql.json', headers={'X-Shopify-Access-Token': data['access_token']}, json={'query': 'mutation { storefrontAccessTokenCreate(input: {title: "Auteric Commerce"}) { userErrors { field message } shop { currencyCode } storefrontAccessToken { accessToken } } }'})
            payload = result.get('data', {}).get('storefrontAccessTokenCreate')
            if result.get('errors') or not payload or payload.get('userErrors') or not payload.get('storefrontAccessToken'):
                raise ValueError('provisioning refused')
            data['storefront_token'] = payload['storefrontAccessToken']['accessToken']
            currency = payload['shop']['currencyCode']
            if not re.fullmatch('[A-Z]{3}', currency) or not data['storefront_token']:
                raise ValueError('invalid storefront credential')
            with dbs.db() as db:
                changed = db.execute("UPDATE shopify_installations SET secrets=?,currency=?,status='connected',updated=? WHERE store=? AND status='provisioning' AND secrets=?", (seal(store_id, row['shop'], data), currency, time.time(), store_id, row['secrets'])).rowcount
                if not changed:
                    raise ValueError('installation changed')
                db.execute(
                    'UPDATE stores SET connector_version=?,connector_release=?,agent_access_enabled=0 WHERE id=?',
                    (HOSTED_CONNECTOR_VERSION, release, store_id),
                )
        except Exception:
            with dbs.db() as db:
                db.execute("UPDATE shopify_installations SET status='provisioning_unknown' WHERE store=? AND status='provisioning'", (store_id,))
            raise HTTPException(502, 'Storefront provisioning did not complete; inspect installation before reconnecting') from None
        exposure = await scan_exposure(store_id)
        return {'connected': True, 'currency': currency, 'agent_access_enabled': False,
                'connector_mode': 'auteric_hosted', 'merchant_worker_required': False,
                'native_exposure': {'state': exposure['state'], 'checked': exposure['checked']},
                'mapping_drafts_created': draft_native_mappings(store_id, actor)}

    @app.post(root + '/prepare')
    async def prepare(store_id: str, body: LiveAcceptanceInput, actor=Depends(app.state.user_dependency)):
        store = app.state.owned(store_id, actor)
        require_installation(store_id)
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM shopify_preparation WHERE store=?', (store_id,)).fetchone()
            if previous:
                if previous['state'] == 'passed':
                    recorded = json.loads(previous['result'])
                    if recorded.get('connector_release') != store['connector_release'] or recorded.get('environment') != store['environment']:
                        raise HTTPException(409, 'Preparation evidence belongs to a different connector release or environment; review before retry')
                    if recorded.get('tested_product', {}).get('id') != body.product_id:
                        raise HTTPException(409, 'Preparation already completed with a different test product')
                    return {**json.loads(previous['result']), 'replayed': True}
                if previous['state'] != 'retry_authorized':
                    raise HTTPException(409, 'Shopify preparation requires review before retry; an earlier run may have written to the store')
                db.execute("UPDATE shopify_preparation SET state='running',result=NULL,updated=? WHERE store=?",
                           (time.time(), store_id))
            else:
                db.execute('INSERT INTO shopify_preparation VALUES(?,?,?,?)',
                           (store_id, 'running', None, time.time()))
        try:
            result = await (prepare_production_capabilities(store_id, actor, body.product_id)
                            if store['environment'] == 'production'
                            else prepare_capabilities(store_id, actor, body.product_id))
            result.update(connector_release=store['connector_release'], environment=store['environment'])
        except BaseException:
            # Includes cancellation/process shutdown. Never automatically repeat
            # merchant writes after an incomplete journey.
            with dbs.db() as db:
                db.execute("UPDATE shopify_preparation SET state=?,updated=? WHERE store=?",
                           ('retry_authorized' if store['environment'] == 'production' else 'needs_review',
                            time.time(), store_id))
            raise
        with dbs.db() as db:
            db.execute("UPDATE shopify_preparation SET state='passed',result=?,updated=? WHERE store=?",
                       (encode(result), time.time(), store_id))
        installed_at = installation(store_id)['installed']
        dbs.event(store['org'], store_id, actor['id'], 'shopify.aha_moment_reached',
                  {'event': 'protected_capabilities_validated',
                   'seconds_from_install': max(0, round(time.time() - installed_at, 3)),
                   'environment': store['environment']})
        return result

    @app.get(root + '/test-products')
    async def test_products(store_id: str, query: str = '', actor=Depends(app.state.user_dependency)):
        app.state.owned(store_id, actor)
        require_installation(store_id)
        if len(query) > 200:
            raise HTTPException(422, 'Search query is too long')
        try:
            products = await asyncio.wait_for(app.state.shopify_hosted_execute(
                store_id, 'search_products', {'query': query, 'limit': 10},
                {'kind': 'sdk', 'operation': 'search_products'}), timeout=20)
        except Exception:
            raise HTTPException(502, 'Could not load the Shopify catalog. No cart was created; retry the read.') from None
        products = [item.model_dump(mode='json') if hasattr(item, 'model_dump') else item for item in products]
        return {'products': [item for item in products if item.get('availability') == 'in_stock' and item.get('sku')],
                'sample_limit': 10, 'full_catalog': False, 'read_only': True}

    async def prepare_capabilities(store_id, actor, product_id):
        """Validate and activate the reviewed hosted Shopify adapter in sandbox.

        A sellable product is selected from Shopify itself. The journey creates
        disposable carts and a hosted checkout handoff but never opens checkout,
        submits payment, or creates an order. Payment-bound operations
        (``complete_checkout`` and its dependent ``get_order``) are certified
        against the pinned hosted release instead of executed: the only honest
        sandbox checkout outcome is the merchant handoff, never a fake payment.
        """
        store = app.state.owned(store_id, actor)
        require_installation(store_id)
        if store['environment'] == 'production':
            raise HTTPException(403, 'Prepare Shopify capabilities in sandbox or staging; production writes are disabled')
        draft_native_mappings(store_id, actor)
        # The reviewed hosted surface is the full canonical registry minus the
        # two atomic-cart operations Shopify cannot perform honestly.
        extended = [operation for operation in app.state.inputs
                    if operation not in OPERATIONS and operation not in {'replace_cart_items', 'cancel_cart'}]
        with dbs.db() as db:
            existing = {row['operation'] for row in db.execute(
                "SELECT operation FROM mappings WHERE store=? AND state IN ('draft','active')", (store_id,))}
        for operation in extended:
            if operation not in existing:
                app.state.save_mapping(store_id, operation, {'kind': 'sdk'}, actor)

        def candidate(operation):
            with dbs.db() as db:
                row = db.execute(
                    "SELECT id,state FROM mappings WHERE store=? AND operation=? "
                    "AND state IN ('draft','active') ORDER BY created DESC LIMIT 1",
                    (store_id, operation),
                ).fetchone()
            if not row:
                raise HTTPException(409, f'Missing hosted Shopify capability: {operation}')
            return dict(row)

        activated, evidence, pending = [], [], []

        async def validate(operation, input_data):
            mapping = candidate(operation)
            if mapping['state'] == 'active':
                selected = app.state.selected_mapping(store_id, operation=operation)
                result = await app.state.dispatch(store_id, operation, input_data, selected)
                evidence.append({'operation': operation, 'state': 'already_active'})
                return result
            result = await app.state.test_mapping_for_platform(store_id, mapping['id'], input_data, actor)
            if result.get('status') != 'pass':
                raise HTTPException(502, {'message': f'Shopify capability test failed: {operation}', 'evidence': result})
            pending.append((operation, mapping['id']))
            evidence.append({'operation': operation, 'state': 'tested'})
            return result['response']

        await validate('search_products', {'query': '', 'limit': 10})
        product = await validate('get_product', {'product_id': product_id})
        if product.get('availability') != 'in_stock' or not product.get('sku'):
            raise HTTPException(409, 'Select a published, in-stock Shopify variant with an SKU')
        row = installation(store_id)
        cart = await validate('create_cart', {'currency': row['currency'], 'items': []})
        cart_id = cart['id']
        await validate('get_cart', {'cart_id': cart_id})
        await validate('add_to_cart', {'cart_id': cart_id, 'product_id': product_id, 'quantity': 1})
        await validate('update_cart_item', {'cart_id': cart_id, 'product_id': product_id, 'quantity': 1})
        await validate('apply_discount_code', {'cart_id': cart_id, 'code': 'AUTERIC-PREP'})
        await validate('remove_discount_code', {'cart_id': cart_id, 'code': 'AUTERIC-PREP'})
        await validate('get_shipping_options', {'cart_id': cart_id})
        checkout = await validate('create_checkout', {'cart_id': cart_id})
        checkout_id = checkout['id']
        await validate('get_checkout', {'checkout_id': checkout_id})
        await validate('set_shipping_address', {'checkout_id': checkout_id, 'address': {
            'line1': '1 Preparation Way', 'city': 'Sandbox', 'country': 'US', 'postal_code': '00000'}})
        await validate('select_shipping_option', {'checkout_id': checkout_id, 'option_id': 'standard'})
        await validate('update_checkout', {'checkout_id': checkout_id,
                                           'items': [{'product_id': product_id, 'quantity': 1}]})
        await validate('cancel_checkout', {'checkout_id': checkout_id})
        await validate('remove_from_cart', {'cart_id': cart_id, 'product_id': product_id})
        certified = []
        now = time.time()
        release = hosted_release()
        live_reads = [candidate('search_products')['id'], candidate('get_product')['id']]
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for operation in ('complete_checkout', 'get_order'):
                mapping = candidate(operation)
                if mapping['state'] == 'active':
                    evidence.append({'operation': operation, 'state': 'already_active'})
                    continue
                record = app.state.selected_mapping(store_id, version=mapping['id'])
                certification = {
                    'kind': 'hosted_release_certification', 'status': 'pass',
                    'environment': store['environment'], 'fingerprint': record['fingerprint'],
                    'tested_at': now, 'connector_release': release,
                    'live_read_mappings': live_reads,
                    'reviewer': actor['id'], 'production_mutation_executed': False,
                    'payment_executed': False, 'production_ready': False,
                    'adapter_manifest': manifest_for(operation, record['fingerprint']),
                    'adapter_requirements': activation_requirements(operation),
                }
                db.execute('INSERT INTO shopify_release_certifications VALUES(?,?,?,?) ON CONFLICT(store,mapping) DO UPDATE SET evidence=excluded.evidence,created=excluded.created',
                           (store_id, mapping['id'], encode(certification), now))
                db.execute('UPDATE mappings SET tests=? WHERE store=? AND id=?',
                           (encode(certification), store_id, mapping['id']))
                pending.append((operation, mapping['id']))
                certified.append(operation)
                evidence.append({'operation': operation, 'state': 'certified_release'})
        app.state.activate_mappings_atomic(store_id, [version for _, version in pending], actor)
        activated.extend(operation for operation, _ in pending)
        dbs.event(store['org'], store_id, actor['id'], 'shopify.hosted_capabilities.prepared', {
            'operations': activated, 'product_id': product_id, 'payment_executed': False,
        })
        return {
            'status': 'passed', 'connector_mode': 'auteric_hosted',
            'merchant_worker_required': False, 'activated_capabilities': activated,
            'tested_product': {'id': product_id, 'sku': product['sku']},
            'steps': evidence, 'checkout_opened': False, 'payment_executed': False,
            'certified_without_payment': certified,
        }

    async def prepare_production_capabilities(store_id, actor, product_id):
        """Certify the built-in adapter with live reads and zero production writes."""
        store = app.state.owned(store_id, actor)
        release = hosted_release()
        draft_native_mappings(store_id, actor)

        def candidate(operation):
            with dbs.db() as db:
                row = db.execute("SELECT id FROM mappings WHERE store=? AND operation=? AND state='draft' ORDER BY created DESC LIMIT 1",
                                 (store_id, operation)).fetchone()
            if not row:
                raise HTTPException(409, f'Missing production Shopify capability: {operation}')
            return row['id']

        search_id, product_mapping_id = candidate('search_products'), candidate('get_product')
        search = await app.state.test_mapping_for_platform(store_id, search_id, {'query': '', 'limit': 10}, actor)
        product = await app.state.test_mapping_for_platform(store_id, product_mapping_id,
                                                             {'product_id': product_id}, actor)
        if search.get('status') != 'pass' or product.get('status') != 'pass':
            raise HTTPException(502, 'Production catalog verification failed; no write was executed')
        item = product['response']
        if item.get('availability') != 'in_stock' or not item.get('sku'):
            raise HTTPException(409, 'Select a published, in-stock Shopify variant with an SKU')
        versions = []
        now = time.time()
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for operation in OPERATIONS:
                mapping_id = candidate(operation)
                mapping = app.state.selected_mapping(store_id, version=mapping_id)
                versions.append(mapping_id)
                if operation in {'search_products', 'get_product'}:
                    continue
                evidence = {
                    'kind': 'hosted_release_certification', 'status': 'pass',
                    'environment': 'production', 'fingerprint': mapping['fingerprint'],
                    'tested_at': now, 'connector_release': release,
                    'live_read_mappings': [search_id, product_mapping_id],
                    'reviewer': actor['id'], 'production_mutation_executed': False,
                    'production_ready': False,
                    'adapter_manifest': manifest_for(operation, mapping['fingerprint']),
                    'adapter_requirements': activation_requirements(operation),
                }
                db.execute('INSERT INTO shopify_release_certifications VALUES(?,?,?,?) ON CONFLICT(store,mapping) DO UPDATE SET evidence=excluded.evidence,created=excluded.created',
                           (store_id, mapping_id, encode(evidence), now))
                db.execute('UPDATE mappings SET tests=? WHERE store=? AND id=?',
                           (encode(evidence), store_id, mapping_id))
        app.state.activate_mappings_atomic(store_id, versions, actor)
        dbs.event(store['org'], store_id, actor['id'], 'shopify.hosted_production_capabilities.certified',
                  {'connector_release': release, 'live_reads': ['search_products', 'get_product'],
                   'production_mutation_executed': False})
        return {'status': 'passed', 'connector_mode': 'auteric_hosted',
                'activated_capabilities': list(OPERATIONS), 'tested_product': {'id': product_id, 'sku': item['sku']},
                'steps': [{'operation': 'search_products', 'state': 'tested_live_read'},
                          {'operation': 'get_product', 'state': 'tested_live_read'},
                          {'operation': 'write_capabilities', 'state': 'certified_release'}],
                'checkout_opened': False, 'payment_executed': False,
                'production_mutation_executed': False}

    @app.post(root + '/prepare/recover')
    def recover_preparation(store_id: str, body: PreparationRecoveryInput,
                            actor=Depends(app.state.user_dependency)):
        store = app.state.owned(store_id, actor)
        if not body.acknowledge_abandoned_test_cart:
            raise HTTPException(409, 'Explicit acknowledgement of a possible abandoned test cart is required')
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM shopify_preparation WHERE store=?', (store_id,)).fetchone()
            if not row or row['state'] != 'needs_review':
                raise HTTPException(409, 'Only a failed preparation can be recovered')
            db.execute("UPDATE shopify_preparation SET state='retry_authorized',updated=? WHERE store=?",
                       (time.time(), store_id))
        dbs.event(store['org'], store_id, actor['id'], 'shopify.hosted_capabilities.retry_authorized',
                  {'possible_abandoned_test_cart': True, 'payment_or_order_created': False})
        return {'state': 'retry_authorized', 'payment_or_order_created': False}

    @app.post(root + '/reconcile')
    async def reconcile(store_id: str, actor=Depends(app.state.user_dependency)):
        app.state.owned(store_id, actor)
        release = hosted_release()
        row, data = await admin_token(store_id, recovery=True)
        if row['status'] != 'provisioning_unknown':
            raise HTTPException(409, 'Only uncertain provisioning can be reconciled')
        result = await admin_query(row, data, '''query AutericTokenRecovery {
          shop { currencyCode storefrontAccessTokens(first: 100) {
            nodes { title accessToken } pageInfo { hasNextPage }
          } }
        }''')
        try:
            shop = result['shop']
            tokens = shop['storefrontAccessTokens']
            matches = [token for token in tokens['nodes'] if token['title'] == 'Auteric Commerce']
            if tokens['pageInfo']['hasNextPage'] or len(matches) != 1:
                raise ValueError('ambiguous result')
            token = matches[0]['accessToken']
            currency = shop['currencyCode']
            if not isinstance(token, str) or not token or not re.fullmatch('[A-Z]{3}', currency):
                raise ValueError('invalid credential')
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, 'No unique Storefront token could be recovered; no new token was created') from None
        data['storefront_token'] = token
        with dbs.db() as db:
            changed = db.execute("UPDATE shopify_installations SET secrets=?,currency=?,status='connected',updated=? WHERE store=? AND status='provisioning_unknown' AND secrets=?",
                (seal(store_id, row['shop'], data), currency, time.time(), store_id, row['secrets'])).rowcount
            if changed:
                db.execute(
                    'UPDATE stores SET connector_version=?,connector_release=?,agent_access_enabled=0 WHERE id=?',
                    (HOSTED_CONNECTOR_VERSION, release, store_id),
                )
        if not changed:
            raise HTTPException(409, 'Installation changed during reconciliation')
        return {'connected': True, 'reconciled': True, 'connector_mode': 'auteric_hosted',
                'merchant_worker_required': False, 'mapping_drafts_created': draft_native_mappings(store_id, actor)}

    @app.post(root + '/verify')
    async def verify(store_id: str, actor=Depends(app.state.user_dependency)):
        app.state.owned(store_id, actor)
        row, data = await admin_token(store_id)
        subscriptions, cursor, seen = [], None, set()
        for _ in range(20):
            result = await admin_query(row, data, '''query AutericInstallationEvidence($after: String) {
              currentAppInstallation { accessScopes { handle } }
              webhookSubscriptions(first: 100, after: $after) {
                nodes { topic uri } pageInfo { hasNextPage endCursor }
              }
            }''', {'after': cursor})
            try:
                scopes = {item['handle'] for item in result['currentAppInstallation']['accessScopes']}
                page = result['webhookSubscriptions']
                subscriptions.extend(page['nodes'])
                if not page['pageInfo']['hasNextPage']:
                    break
                cursor = page['pageInfo']['endCursor']
                if not cursor or cursor in seen:
                    raise ValueError('invalid pagination')
                seen.add(cursor)
            except (KeyError, TypeError, ValueError):
                raise HTTPException(502, 'Shopify returned incomplete installation evidence') from None
        else:
            raise HTTPException(502, 'Webhook inventory exceeded the verification limit')
        missing = sorted(SCOPES - scopes)
        with dbs.db() as db:
            changed = db.execute('UPDATE shopify_installations SET scopes=?,updated=? WHERE store=? AND status=? AND secrets=?',
                (encode(sorted(scopes)), time.time(), store_id, row['status'], row['secrets'])).rowcount
            if not changed:
                raise HTTPException(409, 'Installation changed during verification')
            if missing:
                db.execute('UPDATE stores SET agent_access_enabled=0 WHERE id=?', (store_id,))
        expected = {topic.upper().replace('/', '_'): app.state.public_url + '/api/commerce/shopify/webhooks/' + topic
                    for topic in ('app/uninstalled', 'app/scopes_update')}
        hooks = {topic: {'expected_url': uri, 'observed': any(item.get('topic') == topic and item.get('uri') == uri for item in subscriptions)}
                 for topic, uri in expected.items()}
        with dbs.db() as db:
            for topic, hook in hooks.items():
                if hook['observed']:
                    db.execute("INSERT INTO shopify_hook_registration VALUES(?,?,?,'observed',?) ON CONFLICT(store,topic,uri) DO UPDATE SET state='observed',updated=excluded.updated",
                               (store_id, topic, hook['expected_url'], time.time()))
        return {'checked_at': time.time(), 'shop': row['shop'], 'credential_source': 'auteric_installation',
                'scopes': sorted(scopes), 'missing_scopes': missing, 'webhooks': hooks,
                'subscription_count': len(subscriptions), 'inventory_complete': True,
                'webhook_delivery': 'not_verified', 'storefront_runtime': 'not_verified',
                'callback_configuration': 'not_verified_from_admin_api',
                'expected_callback_url': settings()[3]}

    @app.post(root + '/webhooks/register')
    async def register_webhooks(store_id: str, actor=Depends(app.state.user_dependency)):
        evidence = await verify(store_id, actor)
        if evidence['missing_scopes']:
            raise HTTPException(409, 'Restore required Shopify permissions before registering webhooks')
        row, data = await admin_token(store_id)
        results = {}
        for topic, hook in evidence['webhooks'].items():
            uri = hook['expected_url']
            identity = (store_id, topic, uri)
            with dbs.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT status,secrets FROM shopify_installations WHERE store=?', (store_id,)).fetchone()
                if not current or current['status'] != row['status'] or current['secrets'] != row['secrets']:
                    raise HTTPException(409, 'Installation changed during webhook registration')
                if hook['observed']:
                    db.execute("INSERT INTO shopify_hook_registration VALUES(?,?,?,'observed',?) ON CONFLICT(store,topic,uri) DO UPDATE SET state='observed',updated=excluded.updated", (*identity, time.time()))
                    results[topic] = 'observed'
                    continue
                prior = db.execute('SELECT state FROM shopify_hook_registration WHERE store=? AND topic=? AND uri=?', identity).fetchone()
                if prior and prior['state'] in {'pending', 'unknown'}:
                    results[topic] = 'needs_reconciliation'
                    continue
                db.execute("INSERT INTO shopify_hook_registration VALUES(?,?,?,'pending',?) ON CONFLICT(store,topic,uri) DO UPDATE SET state='pending',updated=excluded.updated", (*identity, time.time()))
            # Persist intent before sending. A timeout/crash is never a reason
            # to blindly repeat a mutation; a later inventory can recover it.
            outcome = 'unknown'
            try:
                result = await admin_query(row, data, '''mutation AutericRegisterWebhook(
                  $topic: WebhookSubscriptionTopic!, $subscription: WebhookSubscriptionInput!) {
                  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $subscription) {
                    webhookSubscription { id topic uri } userErrors { field message }
                  }
                }''', {'topic': topic, 'subscription': {'uri': uri, 'format': 'JSON'}})
                payload = result.get('webhookSubscriptionCreate') or {}
                created = payload.get('webhookSubscription') or {}
                if created.get('id') and created.get('topic') == topic and created.get('uri') == uri and not payload.get('userErrors'):
                    outcome = 'observed'
                elif payload.get('userErrors') and not created:
                    outcome = 'rejected'
            except HTTPException:
                pass
            with dbs.db() as db:
                db.execute('UPDATE shopify_hook_registration SET state=?,updated=? WHERE store=? AND topic=? AND uri=? AND state=\'pending\'', (outcome, time.time(), *identity))
            results[topic] = outcome
        latest = installation(store_id)
        if not latest or latest['status'] != row['status'] or latest['secrets'] != row['secrets']:
            raise HTTPException(409, 'Installation changed during webhook registration')
        return {'subscriptions': results, 'registered': all(value == 'observed' for value in results.values()),
                'delivery_verified': False}

    @app.post(root + '/bootstrap')
    async def bootstrap(store_id: str, actor=Depends(app.state.user_dependency)):
        """Complete the server-owned post-OAuth setup without merchant secrets."""
        connection = await connect(store_id, actor)
        try:
            verification = await verify(store_id, actor)
            observed = all(item['observed'] for item in verification['webhooks'].values())
            webhooks = {'registered': observed, 'delivery_verified': False,
                        'state': 'observed' if observed else 'needs_attention'}
        except HTTPException:
            # Connector provisioning is already durable. Surface lifecycle
            # registration as a retryable readiness blocker, not a false total
            # installation failure that invites another token mutation.
            webhooks = {'registered': False, 'delivery_verified': False,
                        'state': 'needs_attention'}
        dbs.event(actor['org'], store_id, actor['id'], 'shopify.onboarding_step_completed',
                  {'step': 'hosted_connection', 'webhooks_registered': webhooks.get('registered') is True})
        return {'connected': True, 'connection': connection, 'webhooks': webhooks,
                'next': 'select_test_product', 'agent_access_enabled': False}

    @app.post(root + '/live-acceptance')
    async def live_acceptance(store_id: str, body: LiveAcceptanceInput, actor=Depends(app.state.user_dependency)):
        """Run non-payment Storefront acceptance with the installed credential.

        This deliberately returns neither Storefront token, cart identity nor
        checkout URL. It proves the API path only; it never opens checkout or
        submits payment.
        """
        app.state.owned(store_id, actor)
        require_installation(store_id)
        row = installation(store_id)
        data = decrypt(row)
        directory = Path(dbs.path).parent / 'shopify-acceptance-carts'
        directory.mkdir(mode=0o700, exist_ok=True)
        connector = ShopifyConnector(row['shop'], data['storefront_token'],
            database=str(directory / (store_id + '.db')), currency=row['currency'])
        report = {'status': 'failed', 'payment_executed': False, 'checkout_opened': False,
                  'shop': row['shop'], 'product_id': body.product_id, 'checks': []}
        cart_id = None
        try:
            product = await connector.get_product({'product_id': body.product_id})
            report['checks'].append({'operation': 'get_product', 'passed': True,
                                     'sku': product['sku'], 'availability': product['availability']})
            if product['availability'] != 'in_stock':
                raise ValueError('Acceptance variant is not available for sale')
            cart = await connector.create_cart({'currency': row['currency'], 'items': []})
            cart_id = cart['id']
            report['checks'].append({'operation': 'create_cart', 'passed': True})
            cart = await connector.add_to_cart({'cart_id': cart_id, 'product_id': product['id'], 'quantity': 1})
            report['checks'].append({'operation': 'add_to_cart', 'passed': len(cart['items']) == 1})
            cart = await connector.get_cart({'cart_id': cart_id})
            report['checks'].append({'operation': 'get_cart', 'passed': cart['items'][0]['quantity'] == 1})
            cart = await connector.update_cart_item({'cart_id': cart_id, 'product_id': product['id'], 'quantity': 2})
            report['checks'].append({'operation': 'update_cart_item', 'passed': cart['items'][0]['quantity'] == 2})
            checkout = await connector.create_checkout({'cart_id': cart_id})
            report['checks'].append({'operation': 'create_checkout_handoff', 'passed': bool(checkout.get('checkout_url'))})
            await connector.remove_from_cart({'cart_id': cart_id, 'product_id': product['id']})
            report['checks'].append({'operation': 'remove_from_cart', 'passed': True})
            report['status'] = 'passed'
        except Exception as exc:
            report['error'] = str(exc)[:200]
        finally:
            await connector.close()
        dbs.event(actor['org'], store_id, actor['id'], 'shopify.live_acceptance',
                  {'status': report['status'], 'checks': report['checks'], 'payment_executed': False})
        if report['status'] != 'passed':
            raise HTTPException(502, {'message': 'Shopify live acceptance failed', 'report': report})
        return report

    @app.get(root + '/webhooks/deliveries')
    def deliveries(store_id: str, actor=Depends(app.state.user_dependency)):
        app.state.owned(store_id, actor)
        row = installation(store_id)
        if not row:
            return {'events': [], 'privacy_requests_pending': 0}
        with dbs.db() as db:
            events = [dict(event) for event in db.execute('SELECT topic,state,created FROM shopify_events WHERE shop=? ORDER BY created DESC LIMIT 50', (row['shop'],))]
            pending = db.execute("SELECT count(*) FROM shopify_events WHERE shop=? AND state='pending_privacy_review'", (row['shop'],)).fetchone()[0]
        return {'events': events, 'limit': 50, 'privacy_requests_pending': pending}

    @app.get(root + '/connector-config')
    def connector_config(store_id: str, request: Request):
        app.state.auth_token(request, 'connector', store_id)
        require_installation(store_id)
        row = installation(store_id)
        return {'shop': row['shop'], 'currency': row['currency'], 'token': decrypt(row)['storefront_token'], 'token_type': 'public'}

    @app.post('/api/commerce/shopify/webhooks/{topic_path:path}')
    async def webhook(topic_path: str, request: Request):
        _, secret, _, _ = settings()
        try:
            if int(request.headers.get('content-length', '0')) > 2_000_000:
                raise HTTPException(413, 'Shopify webhook payload is too large')
        except ValueError:
            raise HTTPException(400, 'Invalid Content-Length') from None
        raw = await request.body()
        if len(raw) > 2_000_000:
            raise HTTPException(413, 'Shopify webhook payload is too large')
        if 'application/json' not in request.headers.get('content-type', '').lower():
            raise HTTPException(415, 'Shopify webhook must use application/json')
        expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        signature = request.headers.get('x-shopify-hmac-sha256', '')
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, 'Invalid Shopify webhook signature')
        topic = request.headers.get('x-shopify-topic', '')
        if topic != topic_path or topic not in {'app/uninstalled', 'app/scopes_update', 'customers/data_request', 'customers/redact', 'shop/redact'}:
            raise HTTPException(400, 'Unsupported Shopify webhook topic')
        shop = shop_domain(request.headers.get('x-shopify-shop-domain'))
        event = request.headers.get('x-shopify-event-id') or request.headers.get('x-shopify-webhook-id', '')
        if not event or len(event) > 200:
            raise HTTPException(400, 'Missing Shopify delivery identity')
        event = digest(shop + ':' + topic + ':' + event)
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(400, 'Invalid Shopify webhook JSON') from None
        if not isinstance(body, dict):
            raise HTTPException(400, 'Shopify webhook JSON must be an object')
        body_hash = hashlib.sha256(raw).hexdigest()
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT body_hash FROM shopify_events WHERE event=?', (event,)).fetchone()
            if previous:
                if previous['body_hash'] != body_hash:
                    raise HTTPException(409, 'Conflicting duplicate webhook')
                return {'received': True, 'duplicate': True}
            row = db.execute('SELECT store FROM shopify_installations WHERE shop=?', (shop,)).fetchone()
            event_shop = shop
            if row and topic in {'app/uninstalled', 'shop/redact', 'app/scopes_update'}:
                sid = row['store']
                db.execute("UPDATE shopify_installations SET status='revoked',secrets=NULL,updated=? WHERE store=?", (time.time(), sid))
                db.execute('UPDATE stores SET agent_access_enabled=0 WHERE id=?', (sid,))
                db.execute('UPDATE credentials SET revoked=? WHERE store=? AND revoked IS NULL', (time.time(), sid))
            if row and topic == 'shop/redact':
                redacted = 'redacted-' + digest(sid)[:24] + '.invalid'
                for table in ('mappings', 'jobs', 'traffic', 'resources', 'resource_locks',
                              'setup_authorizations', 'capability_controls', 'routing_bindings',
                              'mapping_promotions', 'connection_runs', 'shopify_hook_registration',
                              'shopify_preparation', 'shopify_exposure', 'shopify_release_certifications',
                              'shopify_oauth', 'shopify_cart_aliases'):
                    db.execute(f'DELETE FROM {table} WHERE store=?', (sid,))
                db.execute("UPDATE stores SET name='Redacted Shopify Store',domain=?,policy=NULL,verification_token='',verified=NULL,heartbeat=NULL,connector_version=NULL,connector_release=NULL WHERE id=?",
                           (redacted, sid))
                db.execute("UPDATE shopify_installations SET shop=?,status='redacted',scopes='[]',currency=NULL WHERE store=?",
                           (redacted, sid))
                event_shop = redacted
            state = 'processed_no_customer_data' if topic in {'customers/data_request', 'customers/redact'} else ('redacted' if topic == 'shop/redact' else 'processed')
            # Store no customer or order payload, only delivery identity and body digest.
            db.execute('INSERT INTO shopify_events VALUES(?,?,?,?,?,?)', (event, event_shop, topic, body_hash, state, time.time()))
        if row and topic == 'shop/redact':
            for directory in ('shopify-hosted-carts', 'shopify-acceptance-carts'):
                for suffix in ('', '-wal', '-shm'):
                    (Path(dbs.path).parent / directory / (sid + '.db' + suffix)).unlink(missing_ok=True)
        return {'received': True, 'processing_state': state}
