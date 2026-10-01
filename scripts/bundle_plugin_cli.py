"""Package the executable connect workflow inside the skill, without publishing."""
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
dest = root / 'plugins/auteric-kit/skills/auteric-connect/scripts/cli'
names = ('bin', 'src', 'runtime', 'schemas', 'package.json', 'package-lock.json', 'release-manifest.json', 'README.md')
def product_files(directory):
    return {p.relative_to(directory): p.read_bytes() for name in names
            for p in ([directory / name] if (directory / name).is_file() else (directory / name).rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'
            and not any(part in {'node_modules', 'build'} or part.endswith('.egg-info') for part in p.parts)}
if '--check' in sys.argv:
    if product_files(root) != product_files(dest):
        raise SystemExit('plugin CLI bundle drift; regenerate from maintained source')
    print('Plugin CLI bundle matches maintained source')
    raise SystemExit(0)
if dest.exists():
    shutil.rmtree(dest)  # Generated copy, rebuilt exclusively from maintained CLI source.
dest.mkdir(parents=True, exist_ok=True)
for name in names:
    if (root / name).is_dir():
        shutil.copytree(root / name, dest / name, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', 'build', 'node_modules'))
    else:
        shutil.copy2(root / name, dest / name)
print(dest)
