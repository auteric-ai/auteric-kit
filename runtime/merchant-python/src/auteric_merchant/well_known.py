"""Framework-agnostic /.well-known/ucp handler plus FastAPI/Django mounts.

Serves the verified discovery document with Content-Type: application/json,
X-Content-Type-Options: nosniff, an ETag, and Cache-Control max-age <= 30.
Mount this BEFORE any SPA catch-all route so the well-known URL is not
swallowed by the storefront frontend.
"""
from __future__ import annotations

import hashlib

from .discovery import DiscoveryError, DiscoveryService
from .runtime import RawResponse

WELL_KNOWN_PATH = "/.well-known/ucp"


async def well_known_response(service: DiscoveryService) -> RawResponse:
    """Build the well-known response from a DiscoveryService."""
    try:
        body = await service.get_document_bytes()
    except DiscoveryError:
        return RawResponse(
            status=503,
            body=b'{"error":"discovery document unavailable"}',
            headers={
                "content-type": "application/json",
                "x-content-type-options": "nosniff",
                "cache-control": "no-store",
            },
        )
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    return RawResponse(
        status=200,
        body=body,
        headers={
            "content-type": "application/json",
            "x-content-type-options": "nosniff",
            "etag": etag,
            "cache-control": "public, max-age=30",
        },
    )


def create_well_known_router(service: DiscoveryService):
    """FastAPI router serving WELL_KNOWN_PATH. Include it before the SPA
    catch-all:

        app.include_router(create_well_known_router(discovery))
    """
    from fastapi import APIRouter
    from fastapi.responses import Response

    router = APIRouter()

    @router.get(WELL_KNOWN_PATH, include_in_schema=False)
    async def ucp_well_known() -> Response:
        raw = await well_known_response(service)
        return Response(content=raw.body, status_code=raw.status, headers=dict(raw.headers))

    return router


def make_well_known_view(service: DiscoveryService):
    """Django async view serving WELL_KNOWN_PATH. Wire it in urls.py before
    the SPA catch-all:

        path(".well-known/ucp", make_well_known_view(discovery))
    """
    from django.http import HttpResponse

    async def ucp_well_known(request) -> HttpResponse:
        raw = await well_known_response(service)
        response = HttpResponse(raw.body, status=raw.status)
        for name, value in raw.headers.items():
            response[name] = value
        return response

    return ucp_well_known
