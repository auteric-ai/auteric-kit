#!/usr/bin/env python3
"""Fetch a merchant's public UCP document and verify Auteric exposure evidence.

This is a read-only public check. It never calls commerce actions or claims
runtime enforcement from a signed declaration.
"""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from verify_profile import verify


MAX_PROFILE_BYTES = 1_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def normalize_domain(value):
    raw = value.strip().lower().rstrip(".")
    if not raw or "/" in raw or "@" in raw or ":" in raw or " " in raw:
        raise ValueError("Pass an exact hostname, without a URL, port, or path")
    try:
        ascii_name = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid hostname") from exc
    if "." not in ascii_name or len(ascii_name) > 253:
        raise ValueError("Pass a public merchant hostname")
    for label in ascii_name.split("."):
        if not label or len(label) > 63 or label[0] == "-" or label[-1] == "-" or not all(char.isalnum() or char == "-" for char in label):
            raise ValueError("Invalid hostname")
    return ascii_name


def fetch_profile(domain, *, opener=None, timeout=10):
    url = f"https://{domain}/.well-known/ucp"
    client = opener or build_opener(NoRedirect)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Auteric-Kit-Check/0.1"})
    try:
        with client.open(request, timeout=timeout) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            body = response.read(MAX_PROFILE_BYTES + 1)
    except HTTPError as exc:
        return {"url": url, "http_status": exc.code, "state": "blocked" if exc.code in {403, 429, 503} else "unavailable", "reason": f"HTTP {exc.code}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": url, "http_status": None, "state": "unavailable", "reason": str(exc)}
    if status != 200:
        return {"url": url, "http_status": status, "state": "unavailable", "reason": f"HTTP {status}"}
    if len(body) > MAX_PROFILE_BYTES:
        return {"url": url, "http_status": status, "state": "invalid", "reason": "Profile exceeds 1 MB"}
    if content_type not in {"application/json", "application/ucp+json"}:
        return {"url": url, "http_status": status, "state": "invalid", "reason": f"Expected JSON Content-Type, got {content_type or 'none'}"}
    try:
        profile = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"url": url, "http_status": status, "state": "invalid", "reason": "Response is not valid JSON"}
    if not isinstance(profile, dict):
        return {"url": url, "http_status": status, "state": "invalid", "reason": "Profile root must be an object"}
    return {"url": url, "http_status": status, "state": "fetched", "profile": profile}


def check(domain, *, trusted_key=None, expected_kid=None, opener=None):
    result = fetch_profile(domain, opener=opener)
    profile = result.pop("profile", None)
    if profile is not None:
        state, reason = verify(profile, domain, trusted_key, expected_kid)
        result.update(state=state, reason=reason)
        attestation = profile.get("auteric_attestation")
        payload = attestation.get("payload") if isinstance(attestation, dict) else None
        signed_endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
        if isinstance(signed_endpoint, str):
            parsed = urlsplit(signed_endpoint)
            if parsed.scheme == "https" and parsed.hostname:
                result["declared_endpoint"] = signed_endpoint
    result["enforcement_verified"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="Exact merchant hostname")
    parser.add_argument("--public-key", help="Independently trusted Auteric Ed25519 public key, base64url")
    parser.add_argument("--key-id", help="Expected Auteric signing key ID")
    args = parser.parse_args()
    try:
        result = check(normalize_domain(args.domain), trusted_key=args.public_key, expected_kid=args.key_id)
    except ValueError as exc:
        result = {"state": "invalid", "reason": str(exc), "enforcement_verified": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "verified" else 2


if __name__ == "__main__":
    sys.exit(main())
