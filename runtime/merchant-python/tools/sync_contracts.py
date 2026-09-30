"""Vendor the generated Auteric commerce-contract validators into this package.

Copies packages/commerce-contracts/generated/python/auteric_contracts/__init__.py
to src/auteric_merchant/contracts_generated.py so the published wheel is
self-contained (the generated module is dependency-free and single-file).

Usage:
    python tools/sync_contracts.py          # copy
    python tools/sync_contracts.py --check  # exit 1 if drifted (used by tests)
"""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
SOURCE = REPO_ROOT / "packages" / "commerce-contracts" / "generated" / "python" / "auteric_contracts" / "__init__.py"
TARGET = PACKAGE_ROOT / "src" / "auteric_merchant" / "contracts_generated.py"

HEADER = (
    "# Vendored copy of packages/commerce-contracts/generated/python/auteric_contracts/__init__.py\n"
    "# Regenerate with: python packages/merchant-python/tools/sync_contracts.py\n"
    "# Do not edit by hand.\n"
)


def main(argv: list[str]) -> int:
    source_text = SOURCE.read_text(encoding="utf-8")
    target_text = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    desired = HEADER + source_text
    if "--check" in argv:
        if target_text != desired:
            print(f"DRIFT: {TARGET} is out of sync with {SOURCE}; run tools/sync_contracts.py", file=sys.stderr)
            return 1
        print("contracts_generated.py is in sync")
        return 0
    TARGET.write_text(desired, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
