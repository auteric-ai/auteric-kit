"""Launch unchanged Gateway/Scanner applications for the explicit local pilot."""
import argparse,base64,json,multiprocessing,os,secrets,sys,signal,socket
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--directory',required=True);p.add_argument('--port',type=int,default=8100);p.add_argument('--scanner-port',type=int,default=8090);p.add_argument('--mcp-url');a=p.parse_args()
 for port in (a.port,a.scanner_port):
  with socket.socket() as probe:probe.bind(('127.0.0.1',port))
 folder=Path(a.directory).resolve();folder.mkdir(parents=True,exist_ok=True)
 private=folder/'secrets.json'
 if private.exists():values=json.loads(private.read_text())
 else:
  values={name:secrets.token_urlsafe(32) for name in ['execution','attestation','scanner']}
  fd=os.open(private,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as output:json.dump(values,output)
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
 key=Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(values['attestation']+'=='))
 (folder/'discovery-key.json').write_text(json.dumps({'alg':'Ed25519','public_key':base64.urlsafe_b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode().rstrip('=')}))
 kit=Path(__file__).resolve().parents[1];pilot=kit/'runtime/pilot'
 sys.path[:0]=[str(pilot),str(pilot/'packages/commerce-starter/src'),str(kit/'runtime/sdk/src')]
 os.environ.update(AUTERIC_EXECUTION_ED25519_SEED=values['execution'],AUTERIC_ATTESTATION_PRIVATE_KEY=values['attestation'],AUTERIC_ATTESTATION_KEY_ID='pilot-managed-ed25519',SCANNER_SERVICE_KEY=values['scanner'],AUTERIC_SCANNER_READINESS_URL=f'http://127.0.0.1:{a.scanner_port}/internal/readiness',SCANNER_DB=str(folder/'scanner.db'),SCANNER_ALLOW_LOOPBACK_TARGETS='1')
 if a.mcp_url:os.environ['AUTERIC_MCP_PUBLIC_URL']=a.mcp_url
 import uvicorn
 from services.commerce.app import create_app
 if a.mcp_url: os.environ['AUTERIC_FORCE_SECURE_COOKIE']='1'
 gateway=create_app(str(folder/'gateway.db'),a.mcp_url or f'http://127.0.0.1:{a.port}',development=True)
 def scanner():
  sys.path.insert(0,str(pilot/'services/scanner-api'))
  from app import app
  uvicorn.run(app,host='127.0.0.1',port=a.scanner_port,access_log=False)
 # fork uses the configured module paths/environment without an alternate app.
 process=multiprocessing.get_context('fork').Process(target=scanner)
 process.start()
 def stop(_signal,_frame):raise KeyboardInterrupt
 signal.signal(signal.SIGTERM,stop)
 try:uvicorn.run(gateway,host='127.0.0.1',port=a.port,access_log=False)
 finally:
  process.terminate();process.join(5)
  if process.is_alive():process.kill();process.join(5)
if __name__=='__main__':main()
