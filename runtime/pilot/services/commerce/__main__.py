"""Explicit local-only launcher; deployment must configure its own HTTPS origin."""

import argparse

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--database", default=".runtime/commerce/control.db")
    args = parser.parse_args()
    app = create_app(args.database, f"http://127.0.0.1:{args.port}", development=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
