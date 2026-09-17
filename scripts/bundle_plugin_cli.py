"""Package the executable connect workflow inside the skill, without publishing."""
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
dest = root / 'plugins/auteric-kit/skills/auteric-connect/scripts/cli'
if dest.exists():
    shutil.rmtree(dest)  # Generated copy, rebuilt exclusively from maintained CLI source.
dest.mkdir(parents=True, exist_ok=True)
for name in ('bin', 'src', 'runtime'):
    shutil.copytree(root / name, dest / name, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', 'build'))
shutil.copy2(root / 'package.json', dest / 'package.json')
print(dest)
