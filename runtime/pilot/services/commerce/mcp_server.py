"""Separate MCP process sharing the control plane database and canonical Gateway.

Run with --database and --control-url matching the control plane. No console,
credential provisioning, connector polling or authentication routes are served here.
"""

import argparse
import os

import uvicorn

from .app import create_app


def create_mcp_app(database=None, public_url=None, *, development=False):
    app = create_app(database, public_url, development=development)
    # Both transports are shopper runtime endpoints: the legacy Auteric MCP
    # route and the official UCP MCP binding published in /.well-known/ucp.
    app.router.routes = [
        route for route in app.router.routes
        if route.path.startswith("/mcp/") or route.path.startswith("/ucp/")
    ]

    @app.get("/mcp/healthz", include_in_schema=False)
    def health():
        return {"status": "ok", "service": "commerce-mcp"}

    # The generic /mcp/{store_id} GET route must not capture this ALB probe.
    app.router.routes.insert(0, app.router.routes.pop())

    app.title = "Auteric Commerce MCP"
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.getenv("DATABASE_URL") or os.getenv("AUTERIC_COMMERCE_DATABASE"))
    parser.add_argument("--control-url", default=os.getenv("AUTERIC_COMMERCE_PUBLIC_URL"))
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    if not args.database or not args.control_url:
        parser.error("--database and --control-url (or DATABASE_URL and AUTERIC_COMMERCE_PUBLIC_URL) are required")
    uvicorn.run(create_mcp_app(args.database, args.control_url, development=args.development), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
