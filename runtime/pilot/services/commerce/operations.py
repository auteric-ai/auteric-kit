"""Operational controls for the existing gateway; no merchant credentials or proxy."""
import json
import logging
import os
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .storage import digest, encode, request_trace, uid

logger = logging.getLogger('auteric.gateway')
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)
logger.propagate = False


def attach_operations(app, dbs):
    state = app.state

    def operations_identity(request: Request):
        expected = os.environ.get('AUTERIC_OPERATIONS_TOKEN', '')
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if len(expected) < 32 or not secrets.compare_digest(expected, supplied):
            raise HTTPException(401, 'Operations credential required')
        return 'platform-operator'

    def require_running():
        with dbs.db() as db:
            row = db.execute("SELECT enabled FROM gateway_controls WHERE id='global'").fetchone()
        if os.getenv('AUTERIC_GATEWAY_DISABLED') == 'true' or (row and not row['enabled']):
            raise HTTPException(503, 'Agent commerce is temporarily disabled')

    def configuration():
        from .attestation import private_key
        try:
            private_key(development=False)
            signing = True
        except RuntimeError:
            signing = False
        return {
            'shared_durable_storage': dbs.postgres,
            'https_origin': state.public_url.startswith('https://'),
            'exposure_signing_key': signing,
            'operations_credential': len(os.getenv('AUTERIC_OPERATIONS_TOKEN', '')) >= 32,
            'production_mode': not state.development,
        }

    def require_configuration():
        if not all(configuration().values()):
            raise HTTPException(409, {'message': 'Production runtime configuration is incomplete',
                                     'checks': configuration()})

    state.require_gateway_running = require_running
    state.require_production_configuration = require_configuration

    @app.post('/api/commerce/stores/{store_id}/agent-credentials')
    def issue_agent(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        token = secrets.token_urlsafe(40)
        credential_id = digest(token)
        with dbs.db() as db:
            db.execute('INSERT INTO credentials(hash,store,kind,created,revoked) VALUES(?,?,?,?,NULL)',
                       (credential_id, store_id, 'agent', time.time()))
        dbs.event(actor['org'], store_id, actor['id'], 'agent.credential.created', {'credential_id': credential_id})
        return {'token': token, 'credential_id': credential_id, 'store_id': store_id, 'display_once': True}

    @app.delete('/api/commerce/stores/{store_id}/agent-credentials/{credential_id}')
    def revoke_agent(store_id: str, credential_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            changed = db.execute("UPDATE credentials SET revoked=? WHERE hash=? AND store=? AND kind='agent'",
                                 (time.time(), credential_id, store_id)).rowcount
            if not changed:
                raise HTTPException(404, 'Agent credential not found')
            db.execute('DELETE FROM agent_sessions WHERE store=? AND agent=?', (store_id, credential_id))
        dbs.event(actor['org'], store_id, actor['id'], 'agent.credential.revoked', {'credential_id': credential_id})
        return {'revoked': True}

    class Control(BaseModel):
        model_config = ConfigDict(extra='forbid')
        enabled: bool
        reason: str = Field(min_length=3, max_length=300)

    @app.put('/api/commerce/operations/gateway')
    def control(body: Control, actor=Depends(operations_identity)):
        with dbs.db() as db:
            db.execute("INSERT INTO gateway_controls VALUES('global',?,?) ON CONFLICT(id) "
                       'DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',
                       (int(body.enabled), time.time()))
        dbs.event('platform', None, actor, 'gateway.enabled' if body.enabled else 'gateway.disabled',
                  {'reason': body.reason})
        return {'enabled': body.enabled}

    @app.get('/ready')
    def readiness():
        with dbs.db() as db:
            db.execute('SELECT 1').fetchone()
        require_running()
        if not state.development:
            require_configuration()
        return {'status': 'ready', 'service': 'agent-commerce-gateway'}

    @app.get('/api/commerce/operations/status')
    def status(actor=Depends(operations_identity)):
        with dbs.db() as db:
            uncertain = db.execute("SELECT count(*) FROM jobs WHERE state='uncertain' OR (state='claimed' AND expires<?)", (time.time(),)).fetchone()[0]
            stale = db.execute('SELECT count(*) FROM stores WHERE agent_access_enabled=1 AND (heartbeat IS NULL OR heartbeat<?) AND platform<>?', (time.time()-40, 'shopify')).fetchone()[0]
        return {'checks': configuration(), 'uncertain_jobs': uncertain, 'stale_connectors': stale}

    @app.get('/metrics', response_class=PlainTextResponse)
    def metrics(actor=Depends(operations_identity)):
        snapshot = status(actor)
        lines = [f'auteric_uncertain_jobs {snapshot["uncertain_jobs"]}',
                 f'auteric_stale_connectors {snapshot["stale_connectors"]}']
        with dbs.db() as db:
            for row in db.execute('SELECT status,count(*) AS total FROM gateway_requests WHERE created>? GROUP BY status', (time.time()-300,)):
                lines.append(f'auteric_requests_last_5m{{status="{row["status"]}"}} {row["total"]}')
            for row in db.execute('SELECT state,count(*) AS total FROM traffic GROUP BY state'):
                # State is server-controlled, never a merchant label or URL.
                lines.append(f'auteric_actions{{state="{row["state"]}"}} {row["total"]}')
        return '\n'.join(lines) + '\n'

    @app.middleware('http')
    async def observe(request, call_next):
        trace_id = uid()  # Never trust external identifiers as log content.
        request.state.trace_id = trace_id
        request_trace.set(trace_id)
        started = time.monotonic()
        response = await call_next(request)
        route = request.scope.get('route')
        route_path = getattr(route, 'path', '/unmatched')
        if route_path.startswith(('/ucp/', '/agent-commerce/', '/api/commerce/')):
            params = request.path_params
            sid = params.get('store_id')
            if not sid and params.get('hostname'):
                with dbs.db() as db:
                    bound = db.execute('SELECT store FROM routing_bindings WHERE hostname=?', (params['hostname'],)).fetchone()
                sid = bound['store'] if bound else None
            latency = round((time.monotonic()-started)*1000, 2)
            with dbs.db() as db:
                db.execute('INSERT INTO gateway_requests VALUES(?,?,?,?,?,?,?)',
                           (trace_id, sid, route_path, request.method, response.status_code, latency, time.time()))
            logger.info(encode({'event': 'gateway.request', 'trace_id': trace_id, 'store': sid,
                                'route': route_path, 'method': request.method,
                                'status': response.status_code, 'latency_ms': latency}))
        response.headers['X-Request-ID'] = trace_id
        if not state.development:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.get('/api/commerce/stores/{store_id}/actions/{action_id}/audit')
    def action_audit(store_id: str, action_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            row = db.execute('SELECT * FROM traffic WHERE id=? AND store=?', (action_id, store_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Action not found')
        runtime = state.runtime_class(dbs.runtime_target, store_id, None)
        from .gateway import redact
        with dbs.db() as db:
            events = [dict(r) for r in db.execute('SELECT id,event,data,created FROM audit WHERE store=? ORDER BY id', (store_id,))
                      if json.loads(r['data']).get('action_id') == action_id]
        return {'action_id': action_id, 'state': row['state'], 'operation': row['operation'],
                'events': redact(runtime.audit(action_id)), 'gateway_events': redact(events)}

    @app.post('/api/commerce/stores/{store_id}/actions/{action_id}/reconcile')
    def reconcile(store_id: str, action_id: str, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        from .gateway import redact
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM traffic WHERE id=? AND store=?' +
                             (' FOR UPDATE' if dbs.postgres else ''), (action_id, store_id)).fetchone()
            if not row:
                raise HTTPException(404, 'Action not found')
            if row['state'] == 'executed':
                return {'action_id': action_id, 'state': 'executed', 'replayed': True}
            job = db.execute('SELECT * FROM jobs WHERE id=? AND store=?', (action_id, store_id)).fetchone()
            from .execution_receipts import gateway_receipt
            native = gateway_receipt(db, store_id, action_id)
            if not native and (not job or job['state'] != 'completed' or not job['result']):
                raise HTTPException(409, 'A validated durable connector receipt is required; no write was retried')
            if row['state'] not in {'uncertain', 'failed', 'executing', 'allowed', 'approved', 'evaluating'}:
                raise HTTPException(409, 'Action cannot be reconciled in this state')
            result = json.loads(native['result'] if native else job['result'])
            if native and native['operation'] != row['operation']:
                raise HTTPException(409, 'Receipt operation mismatch')
            from .gateway import result_identity
            data = json.loads(row['input'])
            expected = data.get('product_id') if row['operation'] == 'get_product' else data.get('checkout_id') if row['operation'] == 'get_checkout' else data.get('cart_id')
            if expected and row['operation'] != 'create_checkout' and result_identity(result, row['operation']) != expected:
                raise HTTPException(409, 'Receipt resource identity mismatch')
            if row['operation'] in {'create_cart', 'create_checkout'}:
                kind = 'cart' if row['operation'] == 'create_cart' else 'checkout'
                resource_id = result_identity(result, row['operation'])
                if not resource_id:
                    raise HTTPException(409, 'Receipt resource identity is missing')
                existing = db.execute('SELECT agent,session FROM resources WHERE store=? AND kind=? AND id=?',
                                      (store_id, kind, resource_id)).fetchone()
                if existing and (existing['agent'] != row['agent'] or existing['session'] != row['session']):
                    raise HTTPException(409, 'Receipt resource belongs to another session')
                db.execute('INSERT INTO resources VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',
                           (store_id, kind, resource_id, row['agent'], row['session']))
                if native:
                    db.execute('INSERT INTO resource_owners VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',
                        (native['installation_id'], kind, resource_id, native['principal'], time.time()))
            db.execute("UPDATE traffic SET state='executed',response=?,error=NULL WHERE id=? AND store=?",
                       (encode(redact(result)), action_id, store_id))
            db.execute('DELETE FROM resource_locks WHERE store=? AND action=?', (store_id, action_id))
            db.execute('INSERT INTO audit(org,store,actor,event,data,created) VALUES(?,?,?,?,?,?)',
                       (store['org'], store_id, actor['id'], 'action.reconciled',
                        encode({'action_id': action_id, 'receipt': native['action_id'] if native else job['id']}), time.time()))
        return {'action_id': action_id, 'state': 'executed', 'receipt': action_id, 'write_retried': False}
