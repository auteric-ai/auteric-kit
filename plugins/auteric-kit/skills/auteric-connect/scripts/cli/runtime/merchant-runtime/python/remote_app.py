"""Existing frozen runtime with centrally durable store implementations.

Only storage wiring changes. Authentication, schemas, policies, merchant execution
and reconciliation remain the frozen Sidecar's responsibility.
"""
import asyncio
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from auteric_merchant import sidecar_app
from auteric_merchant.execution_store import ExecutionRecord, ReserveOutcome, IdempotencyConflict, ExecutionInProgress

class RemoteExecutionStore:
    def __init__(self,url,token,installation):
        parsed=urlsplit(url)
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost'}):raise ValueError('authenticated HTTPS runtime state endpoint required')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('invalid runtime state endpoint')
        self.url,self.token,self.installation=url,token,installation
        self.claims={}
    def call(self,operation,arguments):
        response=httpx.post(self.url,headers={'authorization':'Bearer '+self.token},json={'operation':operation,'arguments':arguments},timeout=10,follow_redirects=False)
        if response.status_code==409:
            if response.json().get('detail')=='IDEMPOTENCY_CONFLICT':raise IdempotencyConflict('conflicting action')
            raise ExecutionInProgress('uncertain action')
        response.raise_for_status();return response.json()
    def record(self,value):
        if value is None:return None
        self.claims[value['action_id']]=value['claim']
        return ExecutionRecord(**{k:v for k,v in value.items() if k!='claim'})
    async def reserve(self,installation_id,action_id,request_hash,operation):
        if installation_id!=self.installation:raise ValueError('installation mismatch')
        claim=secrets.token_urlsafe(32)
        value=await asyncio.to_thread(self.call,'reserve',[action_id,request_hash,operation,claim])
        return ReserveOutcome(self.record(value['record']),replay=value['replay'])
    def scope(self,installation_id):
        if installation_id!=self.installation:raise ValueError('installation mismatch')
    async def complete(self,installation_id,action_id,result):
        self.scope(installation_id)
        await asyncio.to_thread(self.call,'complete',[action_id,self.claims[action_id],result])
    async def mark_uncertain(self,installation_id,action_id):
        self.scope(installation_id)
        await asyncio.to_thread(self.call,'uncertain',[action_id,self.claims[action_id]])
    async def release(self,installation_id,action_id):
        self.scope(installation_id)
        await asyncio.to_thread(self.call,'release',[action_id,self.claims[action_id]])
    async def get(self,installation_id,action_id):
        self.scope(installation_id)
        value=await asyncio.to_thread(self.call,'get',[action_id]);return self.record(value['record'])
    async def check_and_store(self,jti,ttl_seconds):
        return (await asyncio.to_thread(self.call,'nonce',[jti,ttl_seconds]))['fresh']
    async def check_ready(self):
        value=await asyncio.to_thread(self.call,'ready',[])
        if not value.get('ready') or value.get('protocol')!='auteric-runtime-state/v1':raise RuntimeError('runtime state protocol mismatch')
    def close(self):pass

class RemoteAuditSink:
    def __init__(self,store):self.store=store
    def record(self,event):self.store.call('audit',[secrets.token_urlsafe(24),vars(event)])
    async def check_ready(self):await self.store.check_ready()

_original=sidecar_app.load_sidecar

def load(config_path):
    url=os.environ.get('AUTERIC_RUNTIME_STATE_URL')
    token=os.environ.get('AUTERIC_SIDECAR_GATEWAY_TOKEN')
    if not url or not token:raise RuntimeError('Gateway-backed state and installation credential required; local durable storage is not the default')
    config=json.loads(Path(config_path).read_text())
    operational=config.setdefault('operational',{})
    for key in ('execution_store','audit_store','database_secret_ref'):operational.pop(key,None)
    operational['storage_mode']='durable'
    with tempfile.TemporaryDirectory(prefix='auteric-config-') as directory:
        sanitized=Path(directory)/'sidecar.json';sanitized.write_text(json.dumps(config));sanitized.chmod(0o600)
        sidecar=_original(sanitized)
    store=RemoteExecutionStore(url,token,sidecar.installation.installation_id)
    sidecar.execution_store=sidecar.runtime.execution_store=store
    sidecar.runtime._nonce_cache=store
    sidecar.audit_sink=RemoteAuditSink(store)
    sidecar.durable=True
    return sidecar

sidecar_app.load_sidecar=load
if __name__=='__main__':raise SystemExit(sidecar_app.main())
