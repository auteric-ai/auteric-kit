"""FastAPI driver: mount the MEP/1 execution endpoint with one call.

    from auteric_merchant.fastapi_driver import create_auteric_router

    app.include_router(create_auteric_router(runtime))

The router is mounted at /api/auteric/v1 and forwards the RAW request target
(path + query string before FastAPI/Starlette normalization) and raw body to
MerchantRuntime. Authentication, validation, and idempotency all happen in
the runtime before any adapter runs. Sync adapters are executed in
Starlette's threadpool (run_in_threadpool), so they never block the event
loop.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .runtime import MerchantRuntime, RawRequest

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _decode_wire_bytes(data: bytes) -> str:
    """Reconstruct a str whose .encode('utf-8', 'surrogateescape') yields the
    original wire bytes (valid UTF-8 becomes proper unicode, the canonicalizer
    rejects the rest)."""
    return data.decode("utf-8", "surrogateescape")


def create_auteric_router(runtime: MerchantRuntime, *, prefix: str = "/api/auteric/v1"):
    router = APIRouter(prefix=prefix)

    @router.api_route("/{full_path:path}", methods=_METHODS, include_in_schema=False)
    async def auteric_execute(request: Request, full_path: str) -> Response:
        scope = request.scope
        raw_path = scope.get("raw_path")
        path = _decode_wire_bytes(raw_path) if raw_path is not None else "/" + full_path
        raw_query = _decode_wire_bytes(scope.get("query_string", b""))
        body = await request.body()
        headers = {name.decode("latin-1"): value.decode("latin-1") for name, value in scope.get("headers", [])}
        raw_request = RawRequest(
            method=request.method,
            path=path,
            raw_query_string=raw_query,
            body=body,
            headers=headers,
        )
        raw_response = await runtime.execute(raw_request)
        return Response(
            content=raw_response.body,
            status_code=raw_response.status,
            headers=dict(raw_response.headers),
        )

    return router
