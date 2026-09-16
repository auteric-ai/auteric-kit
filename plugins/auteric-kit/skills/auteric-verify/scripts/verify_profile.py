#!/usr/bin/env python3
"""Offline Auteric exposure verification. Does not verify runtime enforcement."""

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit


def decode_b64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify(profile, domain, trusted_key=None, expected_kid=None):
    if not isinstance(profile, dict):
        return "invalid", "Profile root must be an object"
    signature = profile.get("auteric_attestation") or {}
    declared = profile.get("ucp") or {}
    if not isinstance(signature, dict) or not isinstance(declared, dict):
        return "invalid", "Profile fields must be objects"
    payload = signature.get("payload") or {}
    if not declared:
        return "no_ucp", "UCP declaration absent"
    if not signature:
        return "no_attestation", "Auteric exposure signature absent"
    if not isinstance(payload, dict):
        return "invalid", "Signature payload must be an object"
    if payload.get("kind") != "auteric.ucp.exposure.v1" or payload.get("domain") != domain:
        return "invalid", "Signature payload kind or domain mismatch"
    endpoint = payload.get("endpoint") or ""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "invalid", "Signed endpoint must be absolute HTTPS"
    services = declared.get("services") or {}
    if not isinstance(services, dict):
        return "invalid", "UCP services must be an object"
    declared_services = services.get("dev.ucp.shopping") or []
    if not isinstance(declared_services, list):
        return "invalid", "Shopping services must be a list"
    declared_endpoints = [entry.get("endpoint") for entry in declared_services if isinstance(entry, dict)]
    if endpoint not in declared_endpoints:
        return "invalid", "Signed endpoint does not match the advertised shopping service"
    if signature.get("alg") != "Ed25519" or not signature.get("kid"):
        return "invalid", "Unsupported or incomplete signature metadata"
    if expected_kid and signature.get("kid") != expected_kid:
        return "invalid", "Key ID mismatch"
    if not trusted_key:
        return "unverified", "Independent trusted Auteric public key not provided"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        Ed25519PublicKey.from_public_bytes(decode_b64(trusted_key)).verify(decode_b64(signature["signature"]), data)
    except ImportError:
        return "unverified", "Install cryptography to verify Ed25519 signatures"
    except (ValueError, KeyError, TypeError, binascii.Error):
        return "invalid", "Invalid signature or key encoding"
    except InvalidSignature:
        return "invalid", "Ed25519 signature did not verify"
    return "verified", "Signed Auteric exposure verified with the independent key"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path, help="Saved public UCP JSON")
    parser.add_argument("--domain", required=True, help="Exact merchant hostname")
    parser.add_argument("--public-key", help="Independently pinned Auteric Ed25519 key, base64url")
    parser.add_argument("--key-id", help="Expected Auteric key ID")
    args = parser.parse_args()
    try:
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        state, reason = verify(profile, args.domain.lower().rstrip("."), args.public_key, args.key_id)
    except (OSError, json.JSONDecodeError) as exc:
        state, reason = "invalid", str(exc)
    print(json.dumps({"exposure_status": state, "reason": reason, "enforcement_verified": False}))
    return 0 if state == "verified" else 2


if __name__ == "__main__":
    sys.exit(main())
