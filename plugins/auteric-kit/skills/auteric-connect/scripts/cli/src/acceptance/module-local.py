"""Real local container acceptance; reuses Gateway bootstrap, transport and contracts.

Merchant fixtures supply setup paths/data only. No mock transport or synthetic pass evidence.
"""
import asyncio
import hashlib
import json
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient
from services.commerce.app import create_app
from services.commerce.installations import Installation, get_installation_by_id
from services.commerce.transports.base import TransportAction
from services.commerce.transports.native_http import contracts, execution_claims, build_execution_jwt, request_hash

STATE = Path(sys.argv[1])
LAB = json.loads((STATE / 'lab-input.json').read_text())
CONNECTION = json.loads((STATE.parent / 'connection.json').read_text())
OPS = CONNECTION['operations']
RESULTS = []


def save(name, value):
    path = STATE / name
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def wait(predicate, message):
    for _ in range(300):
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError(message)


def checked(response):
    assert response.status_code in (200, 201), (response.status_code, response.text[:1000])
    return response.json()


def scenario(operation, name, check, surface="real_http"):
    started = time.monotonic()
    try:
        value = check()
        RESULTS.append(dict(operation=operation, name=name, outcome='passed', surface=surface, elapsed_ms=round((time.monotonic()-started)*1000)))
        return value
    except Exception as exc:
        RESULTS.append(dict(operation=operation, name=name, outcome='failed', detail=str(exc)[:1000]))
        raise


def main():
    gateway_url = 'http://127.0.0.1:' + str(LAB['gatewayPort'])
    sidecar_url = 'http://127.0.0.1:' + str(LAB['sidecarPort'])
    app = create_app(str(STATE / 'gateway.sqlite'), public_url=gateway_url, development=True)
    try:
        from services.commerce.runtime_protocol import install as install_protocol
        install_protocol(app)
    except ImportError:
        pass  # Explicit compatible development sources may already own this boundary.
    from services.commerce.runtime_state import attach_runtime_state
    attach_runtime_state(app,app.state.store)
    server = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=LAB['gatewayPort'], log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait(lambda: server.started, 'local Gateway did not start')
    try:
        with TestClient(app, headers={'X-Auteric-Console':'1'}) as admin:
            checked(admin.post('/api/commerce/auth/register', json={'email':secrets.token_hex(8)+'@local.example','password':secrets.token_urlsafe(24)}))
            store = checked(admin.post('/api/commerce/stores', json={'name':'Local adapter acceptance','domain':secrets.token_hex(8)+'.example','environment':'sandbox'}))['id']
            # Explicit local-test environment: retains the existing loopback-only transport policy.
            with app.state.store.db() as db:
                db.execute("UPDATE stores SET environment='dev' WHERE id=?", (store,))
            root = '/api/commerce/stores/' + store
            checked(admin.put(root+'/policy', json={'max_quantity_per_item':10}))
            digest = 'sha256:' + hashlib.sha256((STATE.parent/'adapter.mjs').read_bytes()+(STATE.parent/'connection.json').read_bytes()).hexdigest()
            profiles = {op:dict(operation=op, mapping_fingerprint=digest, enabled=False,
                integration_mode='service_bridge', merchant_selection_id='model-reviewed-application-boundary',
                target=dict(base_url='http://127.0.0.1:3101', method='POST', path='/invoke/'+op,
                    auth_scheme='bearer', credential_ref='env:AUTERIC_BRIDGE_TOKEN', credential_header='authorization')) for op in OPS}
            installed = checked(admin.post(root+'/installations', json={'environment':'dev','transport':'native_http','endpoint':sidecar_url,
                'native_runtime':{'binding_digest':digest},
                'operations':[dict(operation=op, contract_digest=contracts().REGISTRY_DIGEST, binding_digest=digest) for op in OPS],
                'sidecar':dict(schema='auteric-sidecar-registration/v1',integration_version='local-module-v1',profiles=profiles,
                    allowed_operations=OPS, storage_mode='ephemeral', storage_backend='sqlite', agent_ingress=True)}))
            iid = installed['id']
            config_url = root+'/installations/'+iid+'/sidecar-config'

            def configuration():
                config = checked(admin.get(config_url))
                config['operational'].update(storage_mode='durable')
                save('sidecar.json', config)
                return config

            configuration()
            token = checked(admin.post(root+'/sidecar/credential'))['token']
            save('lab-scope.json', {'store_id':store,'installation_id':iid,'sidecar_token':token})
            wait(lambda:(STATE/'runtime-ready.json').exists(), 'generic runtime did not start')
            installation = Installation.from_row(get_installation_by_id(app.state.store, iid))
            principal = 'guest_' + secrets.token_hex(16)

            def invoke(op, data, *, buyer=principal, action=None, bootstrap=False):
                result = asyncio.run(app.state.native_transport.execute(installation, TransportAction(op, data, buyer,
                    action or 'action_'+secrets.token_hex(12), contracts().OPERATIONS[op]['contract_version'], digest, operator_test=bootstrap)))
                if result.outcome == 'completed':
                    contracts().validate_output(op, result.result)
                return result

            def success(op, data, **kwargs):
                result = invoke(op, data, **kwargs)
                assert result.outcome == 'completed', (op, result.outcome, result.error)
                return result.result

            for op, vectors in LAB['negative_vectors'].items():
                for vector in vectors:
                    def negative(op=op,vector=vector):
                        data=vector['input']
                        try:contracts().validate_input(op,data)
                        except Exception as exc:
                            assert exc.category==vector['error_category'],str(exc)
                        else:raise AssertionError('locked negative vector was accepted')
                        contract=contracts().OPERATIONS[op]
                        if not isinstance(data,dict) or (contract['method']=='GET' and vector['error_category']=='invalid_type'):
                            return # HTTP query values are intentionally coerced; this vector tests the JSON validator
                        path=__import__('re').sub(r'\{([a-z_]+)\}',lambda m:('prod_' if m[1]=='product_id' else 'cart_')+'00000000',contract['path'])
                        method=contract['method']
                        query=urlencode({k:(v if isinstance(v,str) else json.dumps(v,separators=(',',':'))) for k,v in data.items()}) if method=='GET' else ''
                        body=b'' if method=='GET' else json.dumps(data,separators=(',',':')).encode()
                        digest_value='sha256:'+request_hash(method,path,query,body)
                        action=TransportAction(op,{},principal,'action_'+secrets.token_hex(12),contract['contract_version'],digest,operator_test=True)
                        kid,key=app.state.native_transport._signing_key(installation)
                        jwt=build_execution_jwt(key,kid,execution_claims(installation,action,digest_value,'attempt_'+secrets.token_hex(12),app.state.native_transport.issuer))
                        status,payload=asyncio.run(app.state.native_transport._send(installation,method,path,query,body,jwt,10))
                        assert status==400 and json.loads(payload)['error']['code']=='INVALID_INPUT',(status,payload)
                    validator_only=not isinstance(vector['input'],dict) or (contracts().OPERATIONS[op]['method']=='GET' and vector['error_category']=='invalid_type')
                    scenario(op,'conformance_'+vector['name'],negative,surface='contract_validator' if validator_only else 'real_http')
            selected = success('search_products', {'q':LAB['selection']['query']}, bootstrap=True)['results'][0]
            connection_run = checked(admin.post(root+'/test-transaction',json={'bootstrap_sidecar':True,
                'product_id':selected['product_id'],'query':LAB['selection']['query']}))
            assert connection_run['state']=='passed', json.dumps(connection_run)[:4000]
            assert connection_run['phase1_probes']['passed']
            save('connection-run.json', connection_run)
            config = configuration()
            assert set(config['operational']['policy']['allowed_operations']) == set(OPS)
            container = json.loads((STATE/'runtime-ready.json').read_text())['name']

            def restart():
                subprocess.run(['docker','restart',container],check=True,stdout=subprocess.DEVNULL)
                def ready():
                    try:return httpx.get(sidecar_url+'/health/live',timeout=1).is_success
                    except httpx.HTTPError:return False
                wait(ready,'generic runtime restart failed')

            restart()
            product = scenario('get_product','canonical_product_mapping',lambda:success('get_product',{'product_id':selected['product_id']}))
            variant = next(v for v in product['variants'] if v['available'])
            assert variant['price']=={'currency':LAB['selection']['currency'],'amount_minor':LAB['selection']['expectedPriceMinor']}
            scenario('search_products','canonical_search',lambda:success('search_products',{'q':LAB['selection']['query'],'limit':1}))
            def empty_catalog():
                value=success('search_products',{'q':secrets.token_hex(30)})
                assert value['results']==[] and value['page']['has_more'] is False
            scenario('search_products','empty_catalog_result',empty_catalog)
            def wrong_currency():
                result=invoke('create_cart',{'currency':'JPY' if LAB['selection']['currency']!='JPY' else 'USD'})
                assert result.outcome=='failed' and result.error['code']=='INVALID_INPUT',result.error
            scenario('create_cart','unsupported_currency',wrong_currency)
            create_action='action_'+secrets.token_hex(12)
            create_input={'currency':LAB['selection']['currency']}
            cart = scenario('create_cart','create_empty_cart',lambda:success('create_cart',create_input,action=create_action))
            assert scenario('create_cart','idempotent_create_retry',lambda:success('create_cart',create_input,action=create_action))==cart
            cid = cart['cart_id']
            get = lambda:success('get_cart',{'cart_id':cid})
            assert scenario('get_cart','retrieve_owned_cart',get)==cart
            add = dict(cart_id=cid, product_id=product['product_id'], variant_id=variant['variant_id'], quantity=1,expected_revision=cart['resource_revision'])
            action = 'action_'+secrets.token_hex(12)
            added = scenario('add_to_cart','add_item_with_revision',lambda:success('add_to_cart',add,action=action))
            assert added['resource_revision']==cart['resource_revision']+1
            assert added['line_items'][0]['unit_price']==variant['price'] and added['line_items'][0]['quantity']==1
            assert scenario('add_to_cart','idempotent_retry',lambda:success('add_to_cart',add,action=action))==added

            def rejected(op, data, code, **kwargs):
                before = get()
                result = invoke(op, data, **kwargs)
                assert result.outcome=='failed' and result.error['code']==code, (op,result.outcome,result.error)
                assert get()==before, 'rejected request mutated the cart'

            scenario('create_cart','idempotent_create_conflict',lambda:rejected('create_cart',{**create_input,'line_items':[]},'IDEMPOTENCY_CONFLICT',action=create_action))
            scenario('add_to_cart','stale_revision_no_mutation',lambda:rejected('add_to_cart',add,'REVISION_CONFLICT'))
            scenario('add_to_cart','idempotency_conflict_no_mutation',lambda:rejected('add_to_cart',{**add,'quantity':2},'IDEMPOTENCY_CONFLICT',action=action))
            fresh = {**add,'expected_revision':added['resource_revision']}
            scenario('add_to_cart','invalid_quantity_no_mutation',lambda:rejected('add_to_cart',{**fresh,'quantity':0},'INVALID_INPUT'))
            scenario('add_to_cart','invalid_product_no_mutation',lambda:rejected('add_to_cart',{**fresh,'product_id':'prod_00000000'},'RESOURCE_NOT_FOUND'))
            scenario('add_to_cart','invalid_variant_no_mutation',lambda:rejected('add_to_cart',{**fresh,'variant_id':'var_00000000'},'RESOURCE_NOT_FOUND'))
            scenario('get_product','invalid_product',lambda:rejected('get_product',{'product_id':'prod_00000000'},'RESOURCE_NOT_FOUND'))
            other = 'guest_'+secrets.token_hex(16)
            scenario('get_cart','buyer_isolation',lambda:rejected('get_cart',{'cart_id':cid},'RESOURCE_NOT_FOUND',buyer=other))
            scenario('add_to_cart','buyer_write_isolation',lambda:rejected('add_to_cart',fresh,'RESOURCE_NOT_FOUND',buyer=other))

            def inventory(quantity):
                return checked(httpx.put(LAB['merchantOrigin']+LAB['selection']['inventoryPath'],
                    headers={'authorization':'Bearer '+LAB['adminToken']},json={'quantity':quantity}))
            inventory(0)
            try:
                scenario('add_to_cart','stock_rejection_no_mutation',lambda:rejected('add_to_cart',fresh,'OUT_OF_STOCK'))
            finally:
                inventory(LAB['selection']['stock'])

            def owned_carts():
                value=app.state.runtime_state.call(iid,'kv_get',['session',[principal]])['value']
                assert value, 'test buyer has no merchant-owned session'
                return checked(httpx.get(LAB['merchantOrigin']+LAB['selection']['cartsPath'],headers={'cookie':value['cookie']}))

            def atomic_create():
                before=owned_carts()
                result=invoke('create_cart',{'currency':LAB['selection']['currency'],'line_items':[
                    {k:v for k,v in fresh.items() if k in ('product_id','variant_id','quantity')},
                    {'product_id':'prod_00000000','quantity':1}]})
                assert result.outcome=='failed' and result.error['code']=='RESOURCE_NOT_FOUND',result.error
                assert owned_carts()==before,'failed seeded cart escaped merchant rollback'
            scenario('create_cart','atomic_seed_rollback',atomic_create)
            restart()
            assert scenario('get_cart','runtime_restart_preserves_identity',get)==added

            # Real MCP socket and unchanged Gateway authorization/policy, not direct Bridge calls.
            grant=checked(admin.post(root+'/mcp-credentials',json={'operations':OPS}))
            headers={'Authorization':'Bearer '+grant['token']}
            endpoint=gateway_url+'/mcp/'+store
            initialized=httpx.post(endpoint,headers=headers,json={'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2025-11-25'}})
            checked(initialized)
            headers.update({'MCP-Session-Id':initialized.headers['MCP-Session-Id'],'MCP-Protocol-Version':'2025-11-25'})
            listed=checked(httpx.post(endpoint,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}))
            assert {t['name'] for t in listed['result']['tools']}==set(OPS)
            def mcp(op, data, reject=False):
                raw=httpx.post(endpoint,headers={**headers,'Idempotency-Key':secrets.token_hex(16)},timeout=35,
                    json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':op,'arguments':data}})
                if reject:
                    assert raw.status_code==409,raw.text
                    return raw.json()
                response=checked(raw)
                result=response['result']
                envelope=json.loads(result['content'][0]['text'])
                assert not result['isError'],envelope
                return envelope['result']
            scenario('search_products','mcp_gateway_search',lambda:mcp('search_products',{'query':LAB['selection']['query']}))
            scenario('get_product','mcp_gateway_product',lambda:mcp('get_product',{'product_id':product['product_id']}))
            mc=scenario('create_cart','mcp_gateway_create',lambda:mcp('create_cart',{'currency':LAB['selection']['currency'],'items':[]}))
            scenario('get_cart','mcp_gateway_get',lambda:mcp('get_cart',{'cart_id':mc['cart_id']}))
            scenario('add_to_cart','mcp_gateway_add',lambda:mcp('add_to_cart',{'cart_id':mc['cart_id'],
                'product_id':product['product_id'],'variant_id':variant['variant_id'],'quantity':1,'expected_revision':mc['resource_revision']}))
            def mcp_stale():
                before=mcp('get_cart',{'cart_id':mc['cart_id']})
                mcp('add_to_cart',{'cart_id':mc['cart_id'],'product_id':product['product_id'],
                    'variant_id':variant['variant_id'],'quantity':1,'expected_revision':mc['resource_revision']},reject=True)
                assert mcp('get_cart',{'cart_id':mc['cart_id']})==before
            scenario('add_to_cart','mcp_stale_revision_no_mutation',mcp_stale)
            discovery=checked(admin.get('/ucp/'+store+'/.well-known/ucp'))
            save('verified-discovery.json',discovery)
            capabilities=json.loads((STATE/'capabilities.json').read_text())
            report=dict(status='passed',operations=OPS,scenarios=RESULTS,bootstrap_probes=connection_run['phase1_probes'],
                capability_classification=capabilities,local_only=True,production_ready=False,
                runtime_image_id=LAB['imageId'],
                runtime_source_commit=LAB['sourceCommit'] if len(LAB['sourceCommit']) == 40 else None,
                gateway_control_image=LAB['sourceCommit'] if '@sha256:' in LAB['sourceCommit'] else None,
                runtime_image_restarts=2,canonical_scenarios=LAB['canonical_scenarios'],scenario_plan=LAB['scenario_plan'],payment_completed=False)
            save('acceptance.json',report)
            print('Local contract acceptance passed:',len(RESULTS),'scenarios; 5 MCP operations; no payment')
    finally:
        server.should_exit=True
        thread.join(timeout=5)


try:
    main()
except Exception as exc:
    save('acceptance.json',dict(status='failed',operations=OPS,scenarios=RESULTS,error=str(exc)[:4000],local_only=True,production_ready=False))
    raise
