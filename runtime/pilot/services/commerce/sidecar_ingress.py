"""Sidecar-first staging ingress around the existing enforcement lifecycle.

Gateway authorization uses the existing policy, approval and revalidation path.
Only its trusted execution callback changes: issue a short-lived signed request,
then await a merchant-authenticated receipt. No public Bridge call is introduced.
Durable execution reservations protect outcomes if the request/process is lost.
"""
import asyncio
import base64
import json
import secrets
import time

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .execution_receipts import save_receipt
from .installations import Installation
from .principals import resolve_principal, record_resource_owner
from .storage import encode, digest
from .transports.base import TransportAction
from .transports.native_http import (_build_request, execution_claims, build_execution_jwt, contracts)
from .transports.request_hash import request_hash

SUPPORTED = {'search_products','get_product','create_cart','get_cart','add_to_cart',
             'update_cart_item','remove_from_cart'}


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: str
    input: dict


class Receipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    outcome: str
    result: dict | None = None
    error_code: str | None = None


def attach_sidecar_ingress(app, dbs):
    state = app.state
    tasks = set()
    prefix = '/api/commerce/stores/{store_id}/sidecar'

    def installation_for(store_id):
        store = state.store_row(store_id)
        # No silent bypass of production discovery or runtime verification.
        # This initial integration explicitly supports dev/staging/sandbox.
        if store['environment'] == 'production':
            raise HTTPException(409,'Sidecar-first production activation requires a deployed acceptance release')
        with dbs.db() as db:
            row = db.execute('SELECT * FROM installations WHERE store_id=? AND environment=? AND revoked_at IS NULL',
                             (store_id,store['environment'])).fetchone()
        if not row or row['transport'] != 'native_http':
            raise HTTPException(409,'An active signed Native installation is required')
        return Installation.from_row(row)

    def authenticate(request, store_id):
        installation = installation_for(store_id)
        token = request.headers.get('x-auteric-sidecar-token','')
        if not state.credential_vault.resolve(token,'sidecar:'+installation.id,store_id):
            raise HTTPException(401,'Invalid or revoked Sidecar credential')
        scope=None
        verification=request.headers.get('x-auteric-verification','')
        if verification:
            with dbs.db() as db:
                scope=db.execute('SELECT * FROM sidecar_test_scopes WHERE token_hash=? AND store_id=? AND installation_id=? AND expires_at>?',
                    (digest(verification),store_id,installation.id,time.time())).fetchone()
            if not scope:raise HTTPException(401,'Invalid or expired exact verification scope')
            if not scope['binding'] or scope['binding'] != state.connection_binding(store_id):
                raise HTTPException(409,'Verification configuration changed')
            agent,session='operator:'+scope['actor'],'connection-test:'+scope['run_id']
        else:
            agent, session = state.gateway_agent(request,store_id)
        return agent, session, installation, scope

    @app.post(prefix+'/credential')
    def credential(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id,actor)
        installation = installation_for(store_id)
        token = secrets.token_urlsafe(40)
        state.credential_vault.rotate(store_id,'sidecar:'+installation.id,token)
        return {'token':token,'kind':'sidecar','store_id':store_id,'installation_id':installation.id}

    async def delegate(store_id, operation, data, mapping, *, key, principal, policy_exempt=False):
        installation = installation_for(store_id)
        transport = state.native_transport
        if transport is None:
            raise HTTPException(503,'Execution signing unavailable')
        from .app import native_input_for_dispatch
        from .installations import native_runtime_binding_digest
        with dbs.db() as db:
            row = db.execute('SELECT * FROM installations WHERE id=?',(installation.id,)).fetchone()
            traffic = db.execute('SELECT agent,session FROM traffic WHERE id=? AND store=?',(key,store_id)).fetchone()
        binding = resolve_principal(dbs,installation.id,principal)
        if binding['revoked_at']:
            raise HTTPException(403,'Principal binding revoked')
        action = TransportAction(operation=operation,input=native_input_for_dispatch(operation,data),
            principal=binding['merchant_principal'],action_id='action_'+key,
            contract_version=contracts().REGISTRY_VERSION,binding_digest=native_runtime_binding_digest(row),operator_test=policy_exempt)
        method,path,query,body,remainder = _build_request(operation,action.input)
        try:
            contracts().validate_input(operation,remainder)
        except Exception:
            raise HTTPException(422,'Input violates the locked merchant contract') from None
        digest = 'sha256:'+request_hash(method,path,query,body)
        reservation = transport._record_action(installation,action,digest)
        if reservation not in {'fresh','retry'}:
            raise HTTPException(409,'Execution already reserved; reconcile instead of issuing another grant')
        try:
            kid,signer = transport._signing_key(installation)
            jti = 'attempt_'+secrets.token_hex(12)
            claims = execution_claims(installation,action,digest,jti,transport.issuer)
            envelope = {'action_id':key,'installation_id':installation.id,'operation':operation,
                'method':method,'path':path,'query':query,'body_base64':base64.b64encode(body).decode(),
                'authorization':'Bearer '+build_execution_jwt(signer,kid,claims),'expires_at':claims['exp']}
            with dbs.db() as db:
                db.execute('INSERT INTO sidecar_grants VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (key,store_id,installation.id,traffic['agent'],traffic['session'],encode(envelope),
                     claims['exp'],'granted',None,None,time.time()))
            transport._mark_executing(installation,action.action_id)
            deadline = time.monotonic()+35
            while time.monotonic()<deadline:
                with dbs.db() as db:
                    received = db.execute('SELECT * FROM sidecar_grants WHERE action_id=?',(key,)).fetchone()
                if received['state']=='completed':
                    return json.loads(received['result'])
                if received['state']=='uncertain':
                    raise HTTPException(504,'Merchant execution uncertain; reconcile by action_id')
                if received['state']=='failed':
                    from .transports.native_http import ERROR_HTTP
                    code=received['error']
                    raise HTTPException(ERROR_HTTP.get(code,502),'Merchant rejected execution: '+str(code))
                await asyncio.sleep(0.02)
            raise HTTPException(504,'Signed grant outcome is uncertain; no mutation retry')
        except BaseException:
            with dbs.db() as db:
                current=db.execute('SELECT outcome FROM execution_actions WHERE installation_id=? AND action_id=?',
                                   (installation.id,action.action_id)).fetchone()
            if current and current['outcome'] not in {'completed','reconciled','failed'}:
                transport._complete_action(installation,action.action_id,'uncertain')
            raise

    @app.post(prefix+'/actions')
    async def start(store_id: str, body: Start, request: Request):
        agent,session,installation,scope=authenticate(request,store_id)
        if body.operation not in SUPPORTED:
            raise HTTPException(422,'Operation is outside the Sidecar-first catalog/cart scope')
        key=request.headers.get('idempotency-key','')
        if not key or len(key)>200:
            raise HTTPException(422,'A stable Idempotency-Key is required')
        if scope and (body.operation!=scope['operation'] or key!=scope['request_key'] or
                digest(encode(body.input))!=scope['input_hash']):
            raise HTTPException(403,'Verification scope does not authorize this exact request')
        aid=digest(store_id+agent+session+key)
        with dbs.db() as db:
            previous=db.execute('SELECT * FROM sidecar_grants WHERE action_id=?',(aid,)).fetchone()
        if previous and previous['state']=='granted':
            raise HTTPException(409,'Grant already issued; inspect the action outcome')
        data=dict(body.input)
        if body.operation=='search_products' and 'q' in data:data['query']=data.pop('q')
        if body.operation=='create_cart' and 'line_items' in data:data['items']=data.pop('line_items')
        task=asyncio.create_task(state.execute_action(store_id,body.operation,data,agent,session,key,executor=delegate,
            protocol='SIDECAR',operator_id=scope['actor'] if scope else None, bootstrap_scope=scope['binding'] if scope else False))
        tasks.add(task)
        def finished(completed):
            tasks.discard(completed)
            if not completed.cancelled():completed.exception()
        task.add_done_callback(finished)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if task.done():
                return {'outcome':task.result()}
            with dbs.db() as db:
                grant=db.execute('SELECT * FROM sidecar_grants WHERE action_id=? AND store_id=?',(aid,store_id)).fetchone()
            if grant and grant['state']=='granted' and not previous:
                return {'grant':json.loads(grant['envelope'])}
            await asyncio.sleep(0.02)
        task.cancel()
        raise HTTPException(504,'Authorization did not complete within its budget')

    @app.post(prefix+'/actions/{action_id}/receipt')
    def receipt(store_id: str,action_id: str,body: Receipt,request: Request):
        agent,session,installation,scope=authenticate(request,store_id)
        if scope and action_id!=digest(store_id+agent+session+scope['request_key']):
            raise HTTPException(403,'Receipt is outside verification scope')
        if body.outcome not in {'completed','failed','uncertain'}:
            raise HTTPException(422,'Invalid receipt outcome')
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            grant=db.execute('SELECT * FROM sidecar_grants WHERE action_id=? AND store_id=?'+
                (' FOR UPDATE' if dbs.postgres else ''),(action_id,store_id)).fetchone()
            if not grant or grant['installation_id']!=installation.id or grant['agent']!=agent or grant['session']!=session:
                raise HTTPException(403,'Receipt authority mismatch')
            envelope=json.loads(grant['envelope'])
            action_row=db.execute('SELECT * FROM execution_actions WHERE installation_id=? AND action_id=?',
                                 (installation.id,'action_'+action_id)).fetchone()
        if body.outcome=='completed':
            try:contracts().validate_output(action_row['operation'],body.result)
            except Exception:raise HTTPException(422,'Receipt violates the locked merchant contract') from None
            action=TransportAction(operation=action_row['operation'],input={},principal=action_row['principal'],
                action_id=action_row['action_id'],contract_version=contracts().REGISTRY_VERSION,binding_digest='')
            try:
                save_receipt(dbs,installation,action,action_row['request_hash'],body.result)
            except ValueError:
                raise HTTPException(409,'Receipt authority or immutable result conflict') from None
            if action.operation in {'create_cart','create_checkout'}:
                from .gateway import result_identity
                kind='cart' if action.operation=='create_cart' else 'checkout'
                record_resource_owner(dbs,installation.id,kind,result_identity(body.result,action.operation),action.principal)
        else:
            # A client cannot downgrade a completed receipt or release an
            # ambiguous outcome using an arbitrary error string.
            if grant['state']=='completed':
                raise HTTPException(409,'Completed receipt is immutable')
            from .transports.native_http import ERROR_HTTP
            if body.outcome=='failed' and body.error_code not in ERROR_HTTP:
                raise HTTPException(422,'A canonical error code is required')
            if body.outcome=='failed' and (time.time()>envelope['expires_at'] or
                    ERROR_HTTP[body.error_code]>=500):
                body.outcome='uncertain'
            state.native_transport._complete_action(installation,action_row['action_id'],body.outcome)
        with dbs.db() as db:
            db.execute('UPDATE sidecar_grants SET state=?,result=?,error=? WHERE action_id=? AND state != ?',
                (body.outcome,encode(body.result) if body.result is not None else None,body.error_code,action_id,'completed'))
        return {'accepted':True,'action_id':action_id}

    @app.get(prefix+'/actions/{action_id}')
    def outcome(store_id: str,action_id: str,request: Request):
        agent,session,installation,scope=authenticate(request,store_id)
        if scope and action_id!=digest(store_id+agent+session+scope['request_key']):
            raise HTTPException(403,'Outcome is outside verification scope')
        with dbs.db() as db:
            row=db.execute('SELECT * FROM traffic WHERE id=? AND store=? AND agent=? AND session=?',
                (action_id,store_id,agent,session)).fetchone()
            if not row:raise HTTPException(404,'Action not found')
            from .execution_receipts import gateway_receipt
            exact=gateway_receipt(db,store_id,action_id)
        return {'action_id':action_id,'state':row['state'],
                'result':json.loads(exact['result']) if exact else None}

    @app.on_event('shutdown')
    async def stop():
        pending=list(tasks)
        for task in pending:task.cancel()
        if pending:await asyncio.gather(*pending,return_exceptions=True)
