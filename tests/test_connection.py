import base64
import io
import json
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/auteric-kit/skills/auteric-verify/scripts"
sys.path.insert(0, str(SCRIPTS))
from check_connection import check, normalize_domain  # noqa: E402
from verify_profile import verify  # noqa: E402


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self.body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()

    def read(self, size):
        return self.body.read(size)


class Opener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.request = None

    def open(self, request, timeout):
        self.request = request
        if self.error:
            raise self.error
        return Response(self.payload)


def fixture():
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.urlsafe_b64encode(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode().rstrip("=")
    payload = {
        "kind": "auteric.ucp.exposure.v1",
        "domain": "store.example",
        "endpoint": "https://gateway.example/agent-commerce/store.example",
        "store_id": "merchant-1",
        "capabilities": ["search_products"],
    }
    signature = base64.urlsafe_b64encode(private_key.sign(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())).decode().rstrip("=")
    profile = {
        "ucp": {"version": "2026-08-25", "services": {"dev.ucp.shopping": [{"endpoint": payload["endpoint"]}]}},
        "auteric_attestation": {"alg": "Ed25519", "kid": "test-key", "payload": payload, "signature": signature},
    }
    return profile, public_key


class ConnectionTests(unittest.TestCase):
    def test_exact_public_https_fetch_and_independent_signature(self):
        profile, public_key = fixture()
        opener = Opener(profile)
        result = check("store.example", trusted_key=public_key, expected_kid="test-key", opener=opener)
        self.assertEqual(result["state"], "verified")
        self.assertFalse(result["enforcement_verified"])
        self.assertEqual(opener.request.full_url, "https://store.example/.well-known/ucp")

    def test_missing_independent_key_does_not_verify(self):
        profile, _ = fixture()
        self.assertEqual(check("store.example", opener=Opener(profile))["state"], "unverified")

    def test_tampering_and_wrong_domain_fail(self):
        profile, public_key = fixture()
        profile["auteric_attestation"]["payload"]["capabilities"].append("complete_checkout")
        self.assertEqual(verify(profile, "store.example", public_key)[0], "invalid")
        self.assertEqual(verify(profile, "other.example", public_key)[0], "invalid")

    def test_malformed_public_profile_fails_closed(self):
        for profile in [[], {"ucp": "bad"}, {"ucp": {"services": "bad"}, "auteric_attestation": {"payload": {}}}]:
            with self.subTest(profile=profile):
                self.assertEqual(check("store.example", opener=Opener(profile))["state"], "invalid")

    def test_challenge_is_not_missing_support(self):
        error = HTTPError("https://store.example/.well-known/ucp", 403, "Forbidden", {}, None)
        self.assertEqual(check("store.example", opener=Opener(error=error))["state"], "blocked")

    def test_domain_input_cannot_redirect_probe(self):
        for value in ["https://store.example", "store.example:8443", "store.example/path", "a@store.example", "localhost"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_domain(value)

    def test_plugin_structure_and_archive(self):
        manifest = json.loads((ROOT / "plugins/auteric-kit/.codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], "auteric-kit")
        subprocess.run([sys.executable, str(ROOT / "scripts/package-openai-skills.py")], check=True, capture_output=True)
        archive = ROOT / "dist" / f"auteric-kit-{manifest['version']}.zip"
        with zipfile.ZipFile(archive) as packaged:
            names = set(packaged.namelist())
        self.assertIn(".codex-plugin/plugin.json", names)
        self.assertIn("skills/auteric-connect/SKILL.md", names)
        self.assertIn("skills/auteric-verify/scripts/check_connection.py", names)
        self.assertFalse(any("__pycache__" in name for name in names))


if __name__ == "__main__":
    unittest.main()
