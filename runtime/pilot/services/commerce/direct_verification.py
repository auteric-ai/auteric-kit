"""Exact-input owner bootstrap transport for the existing Connection Test.

This module owns no run lifecycle, activation or policy implementation. The
existing connection_runs driver remains the sole source of verification state.
"""
import json
import secrets
import time

import httpx
from fastapi import Depends, HTTPException

from .storage import digest, encode
from .installations import Installation
from .transports.native_http import SsrFBlocked, ResponseTooLarge

OPERATIONS = ('search_products', 'get_product', 'create_cart', 'get_cart', 'add_to_cart')


def direct_installation(dbs, store):
    with dbs.db() as db:
        row = db.execute('SELECT * FROM installations WHERE store_id=? AND environment=? AND revoked_at IS NULL',
                         (store['id'], store['environment'])).fetchone()
    if row and row['transport'] == 'native_http' and store['environment'] != 'production':
        binding = json.loads(row['trust_binding'] or '{}')
        if binding.get('sidecar', {}).get('agent_ingress') is True:
            return dict(row)
    return None


def install_direct_verification(app, dbs):
    state = app.state
    root = '/api/commerce/stores/{store_id}/direct'

    @app.get('/api/commerce/direct-support')
    def support():
        from .attestation import private_key, _b64url
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return {'schema': 'auteric-direct/v1', 'operations': list(OPERATIONS), 'production': False,
                'environments': ['dev', 'staging', 'sandbox'], 'ucp_required': True,
                'discovery_trust': {'public_key': _b64url(private_key(development=state.development)
                                    .public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}}

    @app.get(root + '/config')
    def config(store_id: str, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        installation = direct_installation(dbs, store)
        if not installation:
            raise HTTPException(409, 'An explicit direct Sidecar installation is required')
        return state.sidecar_config(store_id, installation['id'], actor)

    async def execute_verification(report, operation, data, key, *, test_run=None):
        store = state.store_row(report['store_id'])
        installation = direct_installation(dbs, store)
        if not installation or installation['id'] != report['installation_id']:
            raise HTTPException(409, 'The reviewed Sidecar installation changed')
        if state.connection_binding(store['id']) != report['binding']:
            raise HTTPException(409, 'The reviewed Connection Test configuration changed')
        profiles = json.loads(installation['trust_binding'])['sidecar']['profiles']
        if operation not in OPERATIONS or operation not in profiles:
            raise HTTPException(403, 'Operation is outside the reviewed bootstrap scope')
        # Convert only the existing Gateway schema into the existing MEP input.
        from .app import native_input_for_dispatch
        wire_data = native_input_for_dispatch(operation, data)
        token = secrets.token_urlsafe(40)
        with dbs.db() as db:
            db.execute('DELETE FROM sidecar_test_scopes WHERE expires_at<?', (time.time(),))
            db.execute('INSERT INTO sidecar_test_scopes VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (digest(token), store['id'], installation['id'], report['actor'],
                        test_run or report['run_id'], operation, digest(encode(wire_data)), key, time.time() + 120, report['binding']))
        try:
            status, body = await state.native_transport._send_authenticated(
                Installation.from_row(installation), 'POST', '/api/auteric/agent/v1/actions/' + operation,
                '', encode(wire_data).encode(), {'x-auteric-verification': token, 'idempotency-key': key}, 50)
            result = json.loads(body)
            if status >= 400 and result.get('state') != 'blocked':
                raise HTTPException(status, result.get('detail') or result.get('error', {}).get('message') or 'Sidecar verification failed')
            return result
        except (SsrFBlocked, ResponseTooLarge):
            raise HTTPException(502, 'Sidecar verification endpoint failed transport safety checks') from None
        except httpx.HTTPError:
            # Never retry: the existing durable execution record owns uncertainty.
            raise HTTPException(504, 'Sidecar outcome is unconfirmed; inspect the retained action') from None
        finally:
            with dbs.db() as db:
                db.execute('DELETE FROM sidecar_test_scopes WHERE token_hash=?', (digest(token),))

    state.execute_sidecar_verification = execute_verification


    async def probe_auth(report, action_id):
        import base64
        installation = direct_installation(dbs, state.store_row(report['store_id']))
        if not installation or installation['id'] != report['installation_id']:
            return False
        with dbs.db() as db:
            row = db.execute('SELECT envelope FROM sidecar_grants WHERE action_id=? AND store_id=? AND installation_id=?',
                             (action_id, report['store_id'], installation['id'])).fetchone()
        if not row:
            return False
        envelope = json.loads(row['envelope'])
        # An expired JWT's 401 cannot establish nonce replay protection.
        from .transports.native_http import decode_execution_jwt
        _header, claims, _message, _signature = decode_execution_jwt(envelope['authorization'].removeprefix('Bearer '))
        if claims['exp'] <= time.time() + 5:
            return False
        target = Installation.from_row(installation)
        send = state.native_transport._send_authenticated
        missing, _ = await send(target, 'POST', '/api/auteric/agent/v1/actions/create_cart', '', b'{}',
                                {'idempotency-key': report['run_id'] + ':missing-auth'}, 10)
        duplicate, _ = await send(target, envelope['method'], envelope['path'], envelope['query'],
                                 base64.b64decode(envelope['body_base64']),
                                 {'authorization': envelope['authorization']}, 10)
        return missing == 401 and duplicate == 401

    state.probe_sidecar_verification_auth = probe_auth
