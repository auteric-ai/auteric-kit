"""Dedicated developer Installation MCP process; shopper Runtime MCP stays separate."""
import argparse
import os

import uvicorn

from .app import create_app


def create_installation_mcp_app(database=None, public_url=None, *, development=False):
    app = create_app(database, public_url, development=development)
    app.router.routes = [route for route in app.router.routes if route.path == "/installation-mcp" or route.path.startswith("/oauth/") or route.path.startswith("/.well-known/oauth-")]
    app.title = "Auteric Installation MCP"
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.getenv("DATABASE_URL") or os.getenv("AUTERIC_COMMERCE_DATABASE"))
    parser.add_argument("--control-url", default=os.getenv("AUTERIC_COMMERCE_PUBLIC_URL"))
    parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    if not args.database or not args.control_url:
        parser.error("--database and --control-url are required")
    uvicorn.run(create_installation_mcp_app(args.database, args.control_url, development=args.development), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
