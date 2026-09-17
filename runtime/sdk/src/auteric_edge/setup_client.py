"""Short-lived Store setup client; never a runtime or merchant credential."""

from __future__ import annotations

import base64
from urllib.parse import urlsplit

import httpx


def api_url_from_authorization(authorization):
    """Read the non-secret API routing hint embedded in a short-lived token."""
    if not isinstance(authorization, str) or not authorization.startswith("at_setup_") or "." not in authorization:
        raise ValueError("Setup authorization does not contain an Auteric API routing hint")
    encoded = authorization.removeprefix("at_setup_").split(".", 1)[0]
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Setup authorization routing hint is invalid") from None


class SetupClient:
    def __init__(self, api_url, authorization, *, development=False, transport=None):
        routed_by_token = not api_url
        api_url = api_url or api_url_from_authorization(authorization)
        parsed = urlsplit(api_url)
        loopback_hint = routed_by_token and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            (parsed.scheme != "https" and not (
                (development or loopback_hint) and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            ))
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Setup API URL must be HTTPS without credentials, query or fragment")
        if not isinstance(authorization, str) or len(authorization) < 32:
            raise ValueError("A short-lived setup authorization is required")
        self.client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": "Bearer " + authorization},
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def context(self):
        response = await self.client.get("/api/commerce/setup/context")
        response.raise_for_status()
        return response.json()

    async def propose_mappings(self, mappings):
        response = await self.client.post("/api/commerce/setup/mappings", json={"mappings": mappings})
        response.raise_for_status()
        return response.json()

    async def complete(self):
        response = await self.client.post("/api/commerce/setup/complete")
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
