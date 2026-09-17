"""Build the portable CLI runtime from a reviewed local SDK checkout."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('sdk', type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
source = args.sdk.resolve()
if not (source / 'src/auteric_edge/onboarding.py').is_file():
    raise SystemExit('Expected SDK checkout with onboarding bridge')
dest = root / 'runtime/sdk'
if dest.exists():
    shutil.rmtree(dest)  # Generated runtime bundle only; merchant source is elsewhere.
dest.mkdir(parents=True, exist_ok=True)
shutil.copy2(source / 'pyproject.toml', dest / 'pyproject.toml')
shutil.copytree(source / 'src/auteric_edge', dest / 'src/auteric_edge', dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
manifest = {p.relative_to(dest).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(dest.rglob('*')) if p.is_file()}
(root / 'runtime/manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Bundled {len(manifest)} SDK files')
