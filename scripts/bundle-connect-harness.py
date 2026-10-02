"""Bundle the qualified generic local Gateway, never merchant code or credentials."""
import argparse,hashlib,json,os,shutil,subprocess,tempfile,zipfile
from pathlib import Path

KIT=Path(__file__).resolve().parents[1]
IMAGE='416153530465.dkr.ecr.us-east-2.amazonaws.com/auteric-commerce-commerce@sha256:1dd4ad83a738f975bb0632f1273dcae3730421ef0301effa41421a34595efbbc'
parser=argparse.ArgumentParser();parser.add_argument('--image',default=IMAGE)
IMAGE=parser.parse_args().image
with tempfile.TemporaryDirectory() as temporary:
 root=Path(temporary)
 container=subprocess.check_output(['docker','create',IMAGE],text=True).strip()
 try:
  for source,target in [('/app/services/commerce','services/commerce'),('/app/auteric_edge','auteric_edge'),('/app/auteric_commerce','auteric_commerce'),('/app/packages/commerce-contracts','packages/commerce-contracts'),('/app/apps/shopify/shopify.app.toml','apps/shopify/shopify.app.toml')]:
   dest=root/target;dest.parent.mkdir(parents=True,exist_ok=True)
   subprocess.run(['docker','cp',container+':'+source,str(dest)],check=True)
 finally:subprocess.run(['docker','rm',container],check=True,stdout=subprocess.DEVNULL)
 (root/'services/__init__.py').write_text('')
 subprocess.run(['docker','run','--rm','--platform','linux/amd64','--user',str(os.getuid())+':'+str(os.getgid()),'-v',str(root)+':/harness:ro','-w','/harness','-e','PYTHONPATH=/harness','--entrypoint','python',IMAGE,'-c',"import tempfile; from services.commerce.app import create_app; app=create_app(tempfile.mktemp(),public_url='http://127.0.0.1:8100',development=True); print('Isolated bundled Gateway startup passed')"],check=True)
 vectors=KIT.parents[1]/'packages/commerce-contracts/vectors'
 shutil.copytree(vectors,root/'packages/commerce-contracts/vectors',dirs_exist_ok=True)
 freeze=subprocess.check_output(['docker','run','--rm','--platform','linux/amd64','--entrypoint','python',IMAGE,'-m','pip','freeze'],text=True)
 (root/'requirements.txt').write_text(freeze)
 archive=KIT/'runtime/connect-harness.zip'
 with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as output:
  for path in sorted(root.rglob('*')):
   if path.is_file() and '__pycache__' not in path.parts and path.suffix not in {'.pyc','.pyo'}:
    info=zipfile.ZipInfo(path.relative_to(root).as_posix(),date_time=(2020,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
    output.writestr(info,path.read_bytes())
 manifest={'schema':'auteric-connect-harness/v1','control_image':IMAGE,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'python_minimum':'3.11','merchant_code_included':False}
 (KIT/'runtime/connect-harness.json').write_text(json.dumps(manifest,indent=2)+'\n')
 print('Bundled qualified local Gateway and contracts:',archive.stat().st_size,'bytes')
