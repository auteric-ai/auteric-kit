"""Opt-in HTTP acceptance against the local platform and real merchant API.

AUTERIC_PLATFORM_ROOT points to a platform checkout. AUTERIC_MERCHANT_ROOT and
AUTERIC_MERCHANT_API select a read-only live merchant. All account/DB/publication
state is isolated. Pairing codes are consumed in memory, never reported.
"""
import functools
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

KIT = Path(__file__).resolve().parents[1]
PLATFORM = Path(os.environ['AUTERIC_PLATFORM_ROOT'])
sys.path[:0] = [str(PLATFORM), str(PLATFORM/'auteric-commerce-starter/src'), str(PLATFORM/'auteric-commerce-sdk/src')]
spec = importlib.util.spec_from_file_location('http_harness', PLATFORM/'tests/commerce/test_cli_catalog_e2e.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def run_connect(merchant, base, frontend, backend, env, admin, allow_pairing, allow_agent):
    args = ['node', str(KIT/'bin/auteric.js'), '--localhost', '--no-browser', '--api-url', base,
            '--store-url', frontend, '--backend-url', backend]
    if not allow_agent:
        args.append('--no-agent')
    child = subprocess.Popen(args, cwd=merchant, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in child.stdout], daemon=True)
    reader.start()
    request_id = None
    paired = False
    deadline = time.monotonic()+720
    try:
        while time.monotonic() < deadline:
            try: line = lines.get(timeout=.1)
            except queue.Empty:
                if child.poll() is not None: break
                continue
            if '/cli/authorize?request=' in line: request_id = line.strip().split('request=')[1]
            match = re.search(r'code in the browser: (\d{4}-\d{4})', line)
            if match:
                assert allow_pairing, 'Unexpected repeated sign-in'
                assert admin.post('/api/commerce/cli/approve', json={'request_id':request_id,'user_code':match[1]}).status_code==200
                paired = True
        assert child.wait(timeout=5)==0, 'Connect failed; inspect private workflow reports'
        if allow_pairing: assert paired
    finally:
        if child.poll() is None: child.terminate(); child.wait(5)
    state = json.loads((merchant/'.auteric/config.json').read_text())
    assert state['integration']=='locally_tested'
    assert state['local_discovery_verified'] is True
    report = json.loads((merchant/'.auteric/validation.json').read_text())
    assert all(c['state']=='executed' for c in report['mcp']['checks'])
    return state


def test_real_store_and_auth_resume(tmp_path):
    source = Path(os.environ['AUTERIC_MERCHANT_ROOT'])
    backend = os.environ['AUTERIC_MERCHANT_API']
    allow_agent = os.environ.get('AUTERIC_ACCEPTANCE_AGENT') == '1'
    merchant = tmp_path/'merchant'
    shutil.copytree(source, merchant, ignore=shutil.ignore_patterns('node_modules','.git','.auteric','.well-known','.env*','dist','build','.agents','.claude','.cursor'))
    connector_fixture = os.environ.get('AUTERIC_ACCEPTANCE_CONNECTOR')
    if connector_fixture:
        (merchant/'.auteric').mkdir(exist_ok=True)
        shutil.copyfile(connector_fixture, merchant/'.auteric/connector.json')
    public = merchant/'public'; public.mkdir(exist_ok=True)
    frontend_server = ThreadingHTTPServer(('127.0.0.1',0), functools.partial(QuietHandler,directory=str(public)))
    thread = threading.Thread(target=frontend_server.serve_forever,daemon=True); thread.start()
    frontend = f'http://127.0.0.1:{frontend_server.server_port}'
    database = str(tmp_path/'control.db')
    env = {**os.environ, 'AUTERIC_PYTHON':sys.executable,'PYTHONPATH':str(KIT/'runtime/sdk/src'),
           'AUTERIC_SESSION_DIRECTORY':str(tmp_path/'sessions'),'AUTERIC_CREDENTIAL_DIRECTORY':str(tmp_path/'private')}
    # Context here is explicitly isolated acceptance work, not a child Connect.
    env.pop('AUTERIC_AGENT_TASK',None)
    try:
        with harness.live_server(lambda origin: harness.create_app(database,origin,development=True)) as (base,app):
            with harness.live_server(lambda origin: harness.create_mcp_app(database,base,development=True)) as (mcp,_):
                app.state.mcp_public_url=mcp
                with httpx.Client(base_url=base,headers={'X-Auteric-Console':'1'},timeout=45) as admin:
                    assert admin.post('/api/commerce/auth/register',json={'email':'acceptance@example.test','password':'isolated-test-password'}).status_code==200
                    state=run_connect(merchant,base,frontend,backend,env,admin,True,allow_agent)
                    before_mappings=admin.get(f"/api/commerce/stores/{state['store_id']}/mappings").json()
                    again=run_connect(merchant,base,frontend,backend,env,admin,False,allow_agent)
                    assert again['store_id']==state['store_id']
                    assert admin.get(f"/api/commerce/stores/{state['store_id']}/mappings").json()==before_mappings
                    assert len(admin.get('/api/commerce/stores').json())==1
                    assert state['mcp_url']==mcp+'/mcp/'+state['store_id']
                    worker=subprocess.Popen(['node',str(KIT/'bin/auteric.js'),'connector'],cwd=merchant,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    try:
                        sid=state['store_id']
                        token=admin.post(f'/api/commerce/stores/{sid}/agent-token').json()['token']
                        with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+token},timeout=35) as client:
                            session=client.post(f'/ucp/{sid}/sessions'); assert session.status_code==200
                            client.headers['X-Auteric-Session']=session.json()['session_id']
                            result=client.post(f'/ucp/{sid}/catalog/search',json={'query':'','pagination':{'limit':2}})
                            assert result.status_code==200, result.text
                            assert result.json()['products']
                        health=json.loads((merchant/'.auteric/health.json').read_text())
                        assert health['status']=='healthy'
                        # Issuing a replacement token revokes the worker's credential.
                        assert admin.post(f'/api/commerce/stores/{sid}/connector-token').status_code==200
                        assert worker.wait(10)!=0
                        health=json.loads((merchant/'.auteric/health.json').read_text())
                        assert health['status']=='authorization_failed'
                    finally: worker.terminate(); worker.wait(10)
    finally: frontend_server.shutdown(); frontend_server.server_close(); thread.join(3)
