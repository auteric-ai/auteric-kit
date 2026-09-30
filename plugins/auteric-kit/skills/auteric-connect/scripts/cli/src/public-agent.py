"""Independent public discovery-to-MCP acceptance client; no owner/Gateway imports."""
import argparse,json,runpy,time
from pathlib import Path
from urllib.parse import urlsplit
import httpx

def main():
 p=argparse.ArgumentParser();p.add_argument('--domain',required=True);p.add_argument('--key',required=True);p.add_argument('--credential',required=True);p.add_argument('--product-id',required=True);p.add_argument('--query',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 key=json.loads(Path(a.key).read_text())['public_key']
 credential=json.loads(Path(a.credential).read_text())
 verify=runpy.run_path(str(Path(__file__).resolve().parents[1]/'runtime/verify_profile.py'))['verify']
 with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
  for attempt in range(30):
   public=client.get('https://'+a.domain+'/.well-known/ucp',headers={'Cache-Control':'no-cache'});public.raise_for_status()
   if 'application/json' in public.headers.get('content-type',''):break
   time.sleep(1)
  else:raise RuntimeError('Discovery must return JSON; reload merchant route or correct edge caching')
  profile=public.json();state,message=verify(profile,a.domain,key)
  if state!='verified':raise RuntimeError(message)
  endpoint=profile['auteric_mcp']['endpoint'];url=urlsplit(endpoint)
  if url.scheme!='https' or url.username or url.password or endpoint!=credential['mcp_url']:raise RuntimeError('Advertised MCP differs from scoped credential')
  headers={'Authorization':'Bearer '+credential['token'],'Accept':'application/json','MCP-Protocol-Version':'2025-11-25'}
  def rpc(method,params,id):
   r=client.post(endpoint,headers=headers,json={'jsonrpc':'2.0','id':id,'method':method,'params':params});r.raise_for_status();body=r.json()
   if 'error' in body:raise RuntimeError('MCP rejected '+method)
   return r,body['result']
  r,_=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'independent-connect-acceptance','version':'1'}},1)
  headers['MCP-Session-Id']=r.headers['MCP-Session-Id']
  _,tools=rpc('tools/list',{},2)
  if 'search_products' not in {t['name'] for t in tools['tools']}:raise RuntimeError('Advertised catalog tool is unavailable')
  _,called=rpc('tools/call',{'name':'search_products','arguments':{'query':a.query,'limit':10}},3)
  outcome=json.loads(called['content'][0]['text'])
  if called.get('isError') or outcome['state']!='executed':raise RuntimeError('Advertised catalog capability did not execute')
  result=outcome['result'];products=result.get('results',result.get('products',[])) if isinstance(result,dict) else result
  if not any(item.get('product_id',item.get('id'))==a.product_id for item in products):raise RuntimeError('Mapped merchant product absent from MCP result')
  evidence={'state':'passed','observed_at':time.time(),'domain':a.domain,'discovery':'verified','mcp_endpoint':endpoint,'operation':'search_products','action_id':outcome['action_id'],'product_id':a.product_id,'products_returned':len(products)}
  Path(a.output).write_text(json.dumps(evidence,indent=2)+'\n')
  client.delete(endpoint,headers=headers)
  print('Independent public UCP → advertised MCP → merchant catalog: passed')
if __name__=='__main__':main()
