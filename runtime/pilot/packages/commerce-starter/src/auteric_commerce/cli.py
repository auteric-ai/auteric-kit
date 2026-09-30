"""Local-first runtime commands. No cloud resources or live merchant writes created."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile

from .actions import CommerceAction
from .domain import Settings


def dev_settings(directory, policy_path=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "config.json"
    if not path.exists():
        config = {"merchant_id": "demo-store", "database": str(directory / "gateway.db"),
                  "mode": "demo", "local_dev": True, "legacy_gateway_enabled": False,
                  "principals": [{"subject": role + "-local", "role": role,
                                  "token": secrets.token_urlsafe(32),
                                  "human_subject": "demo-merchandiser" if role == "agent" else "demo-" + role,
                                  "business_roles": ["merchandising_manager"] if role == "agent" else [],
                                  "agent_id": "claude-merchant-agent" if role == "agent" else None,
                                  "delegation_scopes": ["pricing.read", "pricing.write"] if role == "agent" else None}
                                 for role in ("agent", "approver", "executor")]}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(config, file, indent=2)
    settings = Settings.model_validate_json(path.read_text())
    if settings.mode != "demo" or not settings.local_dev or settings.live_writes or settings.legacy_gateway_enabled:
        raise ValueError("auteric dev requires a dedicated local demo config with legacy routes and live writes disabled")
    if policy_path:
        settings.runtime_policy_path = str(Path(policy_path).resolve())
    return settings


async def isolated_demo():
    """Repeatable SDK integration scenario with a fresh synthetic merchant every run."""
    from .demo import Demo
    from .runtime import Runtime
    from .domain import DomainError
    with tempfile.TemporaryDirectory(prefix="auteric-demo-") as directory:
        backend = Demo(str(Path(directory) / "catalog.db"))
        runtime = Runtime(Path(directory) / "runtime.db", "demo-store", backend, binding={"mode": "demo"})

        def proposal(price):
            return CommerceAction.model_validate({"merchant_id": "demo-store", "type": "price.update",
                "principal": {"subject": "demo-merchandiser", "roles": ["merchandising_manager"]},
                "agent": {"id": "claude-merchant-agent"}, "source": "synthetic-demo",
                "items": [{"resource_id": "summer-shoes", "proposed_after": price}]})

        allowed = await runtime.evaluate(proposal("95"))
        blocked = await runtime.evaluate(proposal("20"))
        pending = await runtime.evaluate(proposal("75"))
        assert allowed["decision"]["outcome"] == "ALLOW"
        assert blocked["decision"]["outcome"] == "BLOCK"
        assert pending["decision"]["outcome"] == "REQUIRE_APPROVAL"
        try:
            await runtime.execute(pending["id"], "demo-executor")
        except DomainError:
            pass
        else:
            raise AssertionError("Unapproved execution was not blocked")
        runtime.approve(pending["id"], pending["digest"], "demo-reviewer")
        executed = await runtime.execute(pending["id"], "demo-executor")
        replay = await runtime.execute(pending["id"], "demo-executor")
        assert replay == executed and backend.get("summer-shoes").price == 75
        result = {"mode": "synthetic; no LLM or Shopify calls", "allowed_5_percent": allowed["decision"]["outcome"],
                  "blocked_80_percent": blocked["decision"], "approval_25_percent": pending["decision"],
                  "execution_state": executed["state"], "final_price": "75.00", "idempotent_replay": True,
                  "audit_events": len(runtime.audit())}
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="auteric", description="Commerce-aware authorization runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    dev = sub.add_parser("dev", help="Start loopback synthetic catalog, approval console and API")
    dev.add_argument("--directory", default=".runtime/dev", help="Dedicated persistent demo directory (existing files preserved)")
    dev.add_argument("--port", type=int, default=8090)
    dev.add_argument("--policy", help="JSON policy configuration")
    sub.add_parser("demo", help="Run a fresh no-key SDK scenario and verify outcomes")
    args = parser.parse_args()
    if args.command == "demo":
        asyncio.run(isolated_demo())
        return
    from .api import create_app
    import uvicorn
    if not 1024 <= args.port <= 65535:
        parser.error("Use a non-privileged TCP port (1024–65535)")
    settings = dev_settings(args.directory, args.policy)
    audit = logging.getLogger("auteric.audit")
    audit.setLevel(logging.INFO)
    if not audit.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit.addHandler(handler)
        audit.propagate = False
    print(f"Auteric local demo: http://127.0.0.1:{args.port}/console\nSynthetic merchant only; no API key needed. Demo credentials stay in the private config.\nSeparate terminal: auteric demo (fresh, isolated verification).", flush=True)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
