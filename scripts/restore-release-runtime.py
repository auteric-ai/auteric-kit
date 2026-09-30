"""Restore generated runtime dependencies for standalone source CI.

The source checkout has no monorepo build inputs. Restore only generated runtime
directories from an immutable, digest-pinned official release, never its CLI
source. Maintained source and tests continue to run from this checkout.
"""
import hashlib
from pathlib import Path
import shutil
import tarfile
import tempfile
from urllib.request import urlopen

URL = 'https://github.com/auteric-ai/auteric-kit/releases/download/connect-minimum-0.6.5-rc.4/auteric-cli-0.6.5-rc.4.tgz'
SHA256 = '177a35f687492f8584acc30b0a6375061290d3e8e3ccb05557d8b73bb99006ef'
GENERATED = ('merchant-node', 'merchant-python', 'pilot', 'pilot-manifest.json', 'verify_profile.py', 'contracts/schemas')


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='auteric-release-runtime-') as folder:
        archive = Path(folder) / 'runtime.tgz'
        with urlopen(URL, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError('Official runtime asset digest does not match the pinned release')
        archive.write_bytes(data)
        with tarfile.open(archive) as bundle:
            bundle.extractall(Path(folder) / 'unpacked', filter='data')
        source = Path(folder) / 'unpacked' / 'package' / 'runtime'
        for name in GENERATED:
            target = root / 'runtime' / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            if (source / name).is_dir():
                shutil.copytree(source / name, target)
            else:
                shutil.copy2(source / name, target)
    print('Restored digest-pinned generated runtime; CLI source remains this checkout')


if __name__ == '__main__':
    main()
