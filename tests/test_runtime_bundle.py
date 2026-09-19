import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_bundle_integrity_and_executable_plugin_entrypoint():
    manifest = json.loads((ROOT / 'runtime/manifest.json').read_text())
    for relative, expected in manifest.items():
        assert hashlib.sha256((ROOT / 'runtime/sdk' / relative).read_bytes()).hexdigest() == expected
    plugin = ROOT / 'plugins/auteric-kit/skills/auteric-connect/scripts/cli'
    for relative in ['bin/auteric.js', 'src/cli.js', 'src/sdk.js', 'src/agent.js', 'src/workflow.js', 'runtime/manifest.json']:
        assert (plugin / relative).read_bytes() == (ROOT / relative).read_bytes()
    for relative in manifest:
        assert (plugin / 'runtime/sdk' / relative).read_bytes() == (ROOT / 'runtime/sdk' / relative).read_bytes()
    assert 'auteric_edge/onboarding.py' in '\n'.join(manifest)
