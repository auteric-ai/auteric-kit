"""Bundle the existing runtimes; no alternate control-plane implementation."""
from pathlib import Path
import shutil
kit=Path(__file__).resolve().parents[1]
repo=kit.parents[1]
copy_ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info','node_modules','build','dist-test','tests','.runtime','.venv','*.db','*.sqlite','*.sqlite-*','.env*')
for source,target in [
 ('packages/merchant-node/dist','runtime/merchant-node/dist'),
 ('packages/merchant-node/package.json','runtime/merchant-node/package.json'),
 ('packages/merchant-python','runtime/merchant-python'),
 ('packages/commerce-contracts/registry/registry.json','runtime/contracts/registry/registry.json'),
 ('packages/commerce-contracts/schemas','runtime/contracts/schemas'),
 ('kits/auteric-kit/plugins/auteric-kit/skills/auteric-verify/scripts/verify_profile.py','runtime/verify_profile.py'),
 ('services/commerce','runtime/pilot/services/commerce'),
 ('apps/shopify/shopify.app.toml','runtime/pilot/apps/shopify/shopify.app.toml'),
 ('apps/web/public/auteric-mark.png','runtime/pilot/apps/web/public/auteric-mark.png'),
 ('services/scanner-api','runtime/pilot/services/scanner-api'),
 ('packages/commerce-starter','runtime/pilot/packages/commerce-starter'),
 ('packages/commerce-contracts/generated/python','runtime/pilot/packages/commerce-contracts/generated/python'),
]:
 src=repo/source;dst=kit/target
 if dst.is_dir():shutil.rmtree(dst)
 elif dst.exists():dst.unlink()
 dst.parent.mkdir(parents=True,exist_ok=True)
 if src.is_dir():shutil.copytree(src,dst,ignore=copy_ignore)
 else:shutil.copy2(src,dst)
import hashlib,json
paths=[p for p in (kit/'runtime').rglob('*') if p.is_file() and not p.name.endswith('.pyc') and p.name != 'pilot-manifest.json']
fingerprints={str(p.relative_to(kit)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
(kit/'runtime/pilot-manifest.json').write_text(json.dumps({'digest':hashlib.sha256(json.dumps(fingerprints,sort_keys=True).encode()).hexdigest(),'files':fingerprints},sort_keys=True)+'\n')
print('Bundled existing Gateway, Scanner, merchant runtimes and contract schemas')
