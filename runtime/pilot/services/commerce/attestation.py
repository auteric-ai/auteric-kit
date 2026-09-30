"""Signed proof for the Auteric-managed surface advertised by a UCP profile."""

import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def private_key(*, development: bool) -> Ed25519PrivateKey:
    configured = os.getenv("AUTERIC_ATTESTATION_PRIVATE_KEY", "").strip()
    if configured:
        try:
            return Ed25519PrivateKey.from_private_bytes(_decode(configured))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("AUTERIC_ATTESTATION_PRIVATE_KEY must be a base64url Ed25519 private key") from exc
    if development:
        # Deterministic test/local key only. Production fails closed without a managed key.
        return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raise RuntimeError("Configure AUTERIC_ATTESTATION_PRIVATE_KEY before publishing discovery")


def sign_exposure(payload: dict, *, development: bool) -> dict:
    key = private_key(development=development)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        "alg": "Ed25519",
        "kid": os.getenv("AUTERIC_ATTESTATION_KEY_ID", "auteric-local-test-ed25519" if development else "auteric-ed25519-v1"),
        "payload": payload,
        "signature": _b64url(key.sign(encoded)),
        # Discovery clients still need an independently pinned key; this is informational.
        "public_key": _b64url(public_key),
    }
