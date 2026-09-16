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
    files = [PLUGIN / ".codex-plugin" / "plugin.json"]
    files += sorted(path for path in (PLUGIN / "skills").rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    DESTINATION.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(DESTINATION, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            info = zipfile.ZipInfo(file.relative_to(PLUGIN).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, file.read_bytes())
    print(DESTINATION)


if __name__ == "__main__":
    main()
