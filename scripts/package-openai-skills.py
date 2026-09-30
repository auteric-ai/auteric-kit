#!/usr/bin/env python3
"""Build a deterministic skills-only Auteric Kit archive."""

import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "auteric-kit"
VERSION = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())["version"]
DESTINATION = ROOT / "dist" / f"auteric-kit-{VERSION}.zip"


def main():
    # Source Git excludes generated runtimes. Stage the complete current CLI
    # from maintained root source and digest-pinned generated dependencies.
    required = ['runtime/merchant-node/dist/bridge.js', 'runtime/merchant-python/pyproject.toml',
                'runtime/pilot-manifest.json', 'runtime/contracts/schemas', 'runtime/verify_profile.py']
    for name in required:
        if not (ROOT / name).exists():
            raise RuntimeError('Cannot package runnable Connect without '+name)
    files = [PLUGIN / ".codex-plugin" / "plugin.json", PLUGIN / ".claude-plugin" / "plugin.json"]
    files += sorted((PLUGIN/"commands").glob("*.md"))
    files += sorted(path for path in (PLUGIN / "skills").rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    # Build CLI entries separately so ignored generated runtime is included and
    # stale nested source cannot override the tested maintained CLI.
    prefix = 'skills/auteric-connect/scripts/cli/'
    files = [f for f in files if not f.relative_to(PLUGIN).as_posix().startswith(prefix)]
    cli_files = [p for name in ['bin', 'src', 'runtime'] for p in (ROOT/name).rglob('*')
                 if p.is_file() and not any(part in {'__pycache__','.pytest_cache','node_modules','build','*.egg-info'} or part.endswith('.egg-info') for part in p.parts)
                 and p.suffix != '.pyc']
    cli_files += [ROOT/'package.json', ROOT/'README.md', ROOT/'LICENSE', ROOT/'PILOT.md']
    DESTINATION.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(DESTINATION, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            info = zipfile.ZipInfo(file.relative_to(PLUGIN).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, file.read_bytes())
        for file in sorted(cli_files):
            info = zipfile.ZipInfo(prefix+file.relative_to(ROOT).as_posix(), date_time=(2026,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info,file.read_bytes())
    print(DESTINATION)


if __name__ == "__main__":
    main()
