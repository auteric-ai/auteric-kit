"""Direct agent ingress: Gateway authorization, local execution, exact receipt.

The agent credential/session and the merchant Sidecar credential have separate
authority. Caller input cannot choose a Gateway, installation or Bridge URL.
"""
import asyncio
import base64
import json
from urllib.parse import urlsplit

from .runtime import RawRequest


class SidecarGatewayClient:
    def __init__(self, sidecar, gateway_url, credential_ref, *, allow_dev_http=False, transport=None):
        parsed=urlsplit(gateway_url)
        if parsed.scheme!='https' and not (allow_dev_http and parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1'}):
            raise ValueError('Gateway ingress requires HTTPS, except explicit dev loopback')
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
            raise ValueError('Gateway must be a pinned bare origin')
        if allow_dev_http and sidecar.installation.environment!='dev':
            raise ValueError('Plain HTTP Gateway is limited to dev installations')
        self.sidecar,self.gateway_url,self.credential_ref=sidecar,gateway_url.rstrip('/'),credential_ref
        self.transport=transport

    @staticmethod
    async def _request(client, method, url, **kwargs):
        import httpx
        async with client.stream(method,url,**kwargs) as response:
            chunks=[]
            size=0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>4*1024*1024:
                    raise ValueError('Gateway response exceeds the bounded limit')
                chunks.append(chunk)
            return httpx.Response(response.status_code,content=b''.join(chunks))

    async def execute(self, operation, data, *, authorization, session, idempotency_key, verification=''):
        import httpx
        if (not verification and (not authorization.startswith('Bearer ') or not session)) or not idempotency_key:
            return 401,{'error':{'code':'UNAUTHENTICATED','message':'Agent credential, session and idempotency key required'}}
        if operation not in self.sidecar.profiles:
            return 403,{'error':{'code':'CAPABILITY_DISABLED','message':'Operation is not installed'}}
        store=self.sidecar.installation.store_id
        root=f'{self.gateway_url}/api/commerce/stores/{store}/sidecar'
        headers={'authorization':authorization,'x-auteric-session':session,'idempotency-key':idempotency_key,
            'x-auteric-sidecar-token':self.sidecar.secret_resolver(self.credential_ref)}
        if verification:headers['x-auteric-verification']=verification
        try:
            async with httpx.AsyncClient(timeout=40,follow_redirects=False,trust_env=False,transport=self.transport) as client:
                started=await self._request(client,'POST',root+'/actions',json={'operation':operation,'input':data},headers=headers)
                if started.status_code!=200:
                    return started.status_code,{'error':{'code':'GATEWAY_REJECTED','message':'Gateway did not authorize execution'}}
                response=started.json()
                if 'outcome' in response:
                    outcome=response['outcome']
                    return (403 if outcome['state']=='blocked' else 202 if outcome['state']=='waiting_for_approval' else 200),outcome
                grant=response['grant']
                if grant['installation_id']!=self.sidecar.installation.installation_id or grant['operation']!=operation:
                    raise ValueError('grant installation/operation mismatch')
                # Pinned runtime verifies signature, route, request hash,
                # identity, binding, temporal validity and replay before Bridge.
                raw=RawRequest(method=grant['method'],path=grant['path'],raw_query_string=grant['query'],
                    body=base64.b64decode(grant['body_base64'],validate=True),headers={'authorization':grant['authorization']})
                result=await self.sidecar.runtime.execute(raw)
                payload=json.loads(result.body)
                error=payload.get('error',{}) if isinstance(payload,dict) else {}
                uncertain=result.status>=500
                receipt={'outcome':'completed','result':payload} if 200<=result.status<300 else {
                    'outcome':'uncertain' if uncertain else 'failed','error_code':error.get('code','UPSTREAM_ERROR')}
                accepted=await self._request(client,'POST',root+'/actions/'+grant['action_id']+'/receipt',json=receipt,headers=headers)
                if accepted.status_code!=200:
                    return 504,{'action_id':grant['action_id'],'state':'uncertain','error':{'code':'EXECUTION_UNCERTAIN',
                        'message':'Local outcome could not be acknowledged; inspect the existing action'}}
                # Result is committed by the existing Gateway execution lifecycle.
                # Never invent an executed state from the local adapter alone.
                for _ in range(100):
                    observed=await self._request(client,'GET',root+'/actions/'+grant['action_id'],headers=headers)
                    if observed.status_code!=200:break
                    outcome=observed.json()
                    if outcome['state'] in {'executed','failed','uncertain'}:
                        return (200 if outcome['state']=='executed' else 504 if outcome['state']=='uncertain' else result.status),outcome
                    await asyncio.sleep(0.02)
                return 202,{'action_id':grant['action_id'],'state':'awaiting_receipt_commit'}
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            return 504,{'state':'uncertain','error':{'code':'EXECUTION_UNCERTAIN',
                'message':'Authorization or receipt channel did not confirm the action; no mutation retry'}}
