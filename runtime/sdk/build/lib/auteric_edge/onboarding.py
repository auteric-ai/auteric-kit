"""Local CLI bridge: inventory, install, exact contract tests and preparation.

JSON input is read from stdin so browser/connector credentials never enter argv.
No push, deployment, production write tests or public activation is performed.
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .agent_setup import install
from .catalog import CatalogConnector, safe_path
from .manual import ManualConnector, load_factory
from .mapping import MappedConnector
from .mapping import mapping_digest
from .repository_inspection import inspect_repository
from .worker import EdgeWorker


def save(root, relative, data):
    path = safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path)


def inventory(root):
    report = inspect_repository(root)
    path = safe_path(root, "agent-catalog.json")
    if path.is_file():
        try:
            catalog = CatalogConnector(root)
            report["catalog"] = {
                "path": "agent-catalog.json",
                "products": len(catalog.products()),
                "status": "available",
            }
            for item in report["capability_coverage"]:
                if item["operation"] in catalog.supported_operations:
                    item.update(status="available", evidence=[{"source": "agent-catalog.json", "line": 1}])
        except (ValueError, OSError) as exc:
            report["catalog"] = {"path": "agent-catalog.json", "status": "invalid", "reason": str(exc)}
    for item in report["capability_coverage"]:
        item["reason"] = (
            "Validated local catalog data; runtime contract checks still required"
            if item["status"] == "available"
            else "Source candidate needs authentication, semantic and response tracing"
            if item["status"] == "candidate"
            else "No supported server-side implementation detected; inspect unmatched code"
        )
        browser = [o for o in report["observations"] if o["kind"] in {"browser_only_cart", "simulated_payment"}]
        if (
            item["status"] == "not_detected"
            and browser
            and ("cart" in item["operation"] or "checkout" in item["operation"])
        ):
            item.update(
                status="unsupported",
                reason="Browser-only state or simulated payment is not an authenticated commerce API",
                evidence=browser,
            )
    report["unsupported_runtime_domains"] = ["payment capture", "orders", "refunds", "identity linking"]
    return report


def _local_catalog_connector(root, report, store_url):
    """Create only a runtime-validated, read-only common catalog adapter."""
    if not store_url:
        return None
    parsed = urlsplit(store_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path not in {"", "/"}:
        return None
    candidates = report.get("candidates", [])
    operations = {item.get("operation") for item in candidates}
    paths = {item.get("behavior") for item in candidates}
    if not {"search_products", "get_product"}.issubset(operations) or not {"GET /api/products", "GET /api/products/:id"}.issubset(paths):
        return None
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = client.get(store_url.rstrip("/") + "/api/products", params={"q": ""})
            response.raise_for_status()
            rows = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    sample = rows[0]
    if not all(isinstance(sample.get(field), str) and sample[field] for field in ("id", "sku", "name", "description")) or not isinstance(sample.get("price"), (int, float)):
        return None
    product = {
        "id": {"source": "id", "kind": "string"}, "sku": {"source": "sku", "kind": "string"},
        "title": {"source": "name", "kind": "string"}, "description": {"source": "description", "kind": "string"},
        "price": {"source": "price", "kind": "number"}, "currency": {"constant": "USD"},
        "availability": {"constant": "unknown"},
    }
    mappings = [
        {"operation": "search_products", "method": "GET", "path": "/api/products", "request": {"query": {"q": {"source": "query", "kind": "string"}}}, "response": product},
        {"operation": "get_product", "method": "GET", "path": "/api/products/{id}", "request": {"path": {"id": {"source": "product_id", "kind": "string"}}}, "response": product},
    ]
    return {
        "kind": "rest", "base_url": store_url.rstrip("/"),
        "allowed_paths": ["/api/products", "/api/products/{id}"],
        "approved_mapping_digests": [mapping_digest(item) for item in mappings], "mappings": mappings,
        "test_inputs": {"search_products": {"query": "", "limit": 2}, "get_product": {"product_id": sample["id"]}},
        "generated": {"kind": "local_read_only_catalog", "verified_url": store_url.rstrip("/") + "/api/products"},
    }


def prepare(root, agent, store_url=None):
    report = inventory(root)
    skill = install(agent, root) if agent in {"codex", "claude-code", "cursor"} else None
    ignore = safe_path(root, ".auteric/.gitignore")
    if not ignore.exists():
        ignore.parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text("jobs.db*\nconfig.json\nvalidation.json\n")
    config_path = safe_path(root, ".auteric/connector.json")
    if not config_path.exists() and report.get("catalog", {}).get("status") == "available":
        save(root, ".auteric/connector.json", {"kind": "catalog", "path": "agent-catalog.json"})
    if not config_path.exists():
        generated = _local_catalog_connector(root, report, store_url)
        if generated:
            save(root, ".auteric/connector.json", generated)
            report["generated_connector"] = {"kind": generated["generated"]["kind"], "operations": ["search_products", "get_product"], "verified_url": generated["generated"]["verified_url"]}
    save(root, ".auteric/capabilities.json", report)
    return {"inventory": report, "skill": skill, "connector_prepared": config_path.exists()}


def connector(root, config, development):
    if config["kind"] == "catalog":
        implementation = ManualConnector(CatalogConnector(root, config["path"]))
        products = implementation.implementation.products()
        mappings = [{"operation": op, "mapping": {"kind": "sdk"}} for op in sorted(implementation.operations)]
        inputs = {"search_products": {"query": "", "limit": 2}, "get_product": {"product_id": products[0]["id"]}}
    elif config["kind"] == "factory":
        # Explicit local module selection, never a remote job or inferred import.
        sys.path.insert(0, str(Path(root).absolute()))
        implementation = load_factory(config["factory"])
        mappings = [{"operation": op, "mapping": {"kind": "sdk"}} for op in implementation.operations]
        inputs = config.get("test_inputs", {})
    elif config["kind"] == "rest":
        implementation = MappedConnector(
            config["base_url"],
            allowed_paths=config["allowed_paths"],
            approved_mapping_digests=config["approved_mapping_digests"],
            credentials={name: os.environ[name] for name in config.get("credential_env", [])},
            allow_loopback=development,
        )
        mappings, inputs = config["mappings"], config.get("test_inputs", {})
    else:
        raise ValueError("Unsupported local connector kind")
    return implementation, mappings, inputs


def runtime_directory():
    root = Path(os.environ.get("AUTERIC_CREDENTIAL_DIRECTORY", Path.home() / ".config/auteric/connectors")).absolute()
    safe_path(root, "check")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def runtime_key(base, sid):
    return hashlib.sha256((base + "/" + sid).encode()).hexdigest()


async def connect(root, settings):
    config_file = safe_path(root, ".auteric/connector.json")
    if not config_file.exists():
        return {
            "status": "implementation_required",
            "tested_operations": [],
            "reason": "Use the installed skill to trace the source candidates and implement .auteric/connector.json; no empty-capability success is reported.",
        }
    config = json.loads(config_file.read_text())
    implementation, mappings, inputs = connector(root, config, settings["development"])
    mode = settings["environment"]
    if mode == "production":
        # Local connect prepares production work; activation/publication is a later gate.
        await implementation.close()
        return {
            "status": "publication_approval_required",
            "prepared_operations": [m["operation"] for m in mappings],
            "tested_operations": [],
        }
    missing = [m["operation"] for m in mappings if m["operation"] not in inputs]
    if missing:
        await implementation.close()
        return {"status": "test_inputs_required", "operations": missing, "tested_operations": []}
    base = settings["api_url"]
    sid = settings["store_id"]
    prefix = f"/api/commerce/stores/{sid}"
    async with httpx.AsyncClient(
        base_url=base,
        headers={"Authorization": "Bearer " + settings["token"], "X-Auteric-Console": "1"},
        timeout=45,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        async def call(path, body=None, method="POST"):
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        issued = await call(prefix + "/connector-token")
        secret_root = runtime_directory()
        secret_name = hashlib.sha256((base + "/" + sid).encode()).hexdigest() + ".json"
        secret_path = safe_path(secret_root, secret_name)
        secret_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {
                    "api_url": base,
                    "store_id": sid,
                    "token": issued["token"],
                    "environment": mode,
                    "development": settings["development"],
                },
                stream,
            )
        worker = EdgeWorker(
            implementation,
            api_url=base,
            store_id=sid,
            token=issued["token"],
            database=str(runtime_directory() / (runtime_key(base, sid) + ".jobs.db")),
            environment=mode,
            allow_loopback=settings["development"],
        )
        task = None
        report = {"status": "testing", "tested_operations": [], "mapping_versions": [], "production_verified": False}
        try:
            await worker.tick()
            task = asyncio.create_task(worker.run())
            for item in mappings:
                # The control plane receives a draft envelope. `item` is the
                # local deterministic REST/SDK mapping, while `operation` is
                # the canonical capability being activated.
                row = await call(prefix + "/mappings", {"operation": item["operation"], "mapping": item})
                evidence = await call(
                    prefix + "/mappings/" + row["id"] + "/test", {"input": inputs[item["operation"]], "mode": mode}
                )
                report["mapping_versions"].append(
                    {"operation": item["operation"], "id": row["id"], "status": evidence["status"]}
                )
                if evidence["status"] != "pass":
                    report["status"] = "contract_failed"
                    return report
                report["tested_operations"].append(item["operation"])
            for row in report["mapping_versions"]:
                await call(prefix + "/mappings/" + row["id"] + "/activate")
            report["status"] = "connection_test_required"
            # Explicit sandbox test_inputs authorize these nonfinancial journeys.
            # Runtime policy may still block or require approval; never bypass it.
            if "get_product" in inputs or set(report["tested_operations"]) == {"search_products"}:
                policy = await call(prefix + "/policy", method="GET")
                await call(prefix + "/policy", policy, method="PUT")
                report["connection_test"] = await call(
                    prefix + "/test-transaction",
                    {"product_id": inputs.get("get_product", {}).get("product_id"), "query": ""},
                )
                if report["connection_test"]["state"] != "passed":
                    report["status"] = "connection_failed"
                else:
                    await call(prefix + "/agent-access", {"enabled": True}, method="PUT")
                    granted = await call(prefix + "/mcp-credentials", {"operations": report["tested_operations"]})
                    try:
                        async with httpx.AsyncClient(
                            headers={
                                "Authorization": "Bearer " + granted["token"],
                                "Accept": "application/json, text/event-stream",
                            },
                            timeout=45,
                            trust_env=False,
                        ) as mcp:
                            # This endpoint comes from the authenticated control plane,
                            # and may point at the separately running MCP service.
                            endpoint = granted["mcp_url"]
                            from .mapping import safe_base_url

                            safe_base_url(endpoint, allow_loopback=settings["development"])
                            started = await mcp.post(
                                endpoint,
                                json={
                                    "jsonrpc": "2.0",
                                    "id": 1,
                                    "method": "initialize",
                                    "params": {
                                        "protocolVersion": "2025-11-25",
                                        "capabilities": {},
                                        "clientInfo": {"name": "auteric-connect", "version": "0.4.0"},
                                    },
                                },
                            )
                            started.raise_for_status()
                            mcp.headers.update(
                                {
                                    "MCP-Session-Id": started.headers["MCP-Session-Id"],
                                    "MCP-Protocol-Version": "2025-11-25",
                                }
                            )
                            await mcp.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                            listed = await mcp.post(endpoint, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                            listed.raise_for_status()
                            names = sorted(t["name"] for t in listed.json()["result"]["tools"])
                            if names != sorted(report["tested_operations"]):
                                raise ValueError("MCP tool list differs from tested capabilities")
                            checks, resources = [], {}
                            order = [
                                "search_products",
                                "get_product",
                                "create_cart",
                                "get_cart",
                                "add_to_cart",
                                "update_cart_item",
                                "remove_from_cart",
                                "replace_cart_items",
                                "create_checkout",
                                "get_checkout",
                                "cancel_cart",
                            ]
                            for index, op in enumerate([op for op in order if op in names], 3):
                                arguments = dict(inputs[op])
                                if op == "create_cart":
                                    arguments["items"] = []
                                if "cart_id" in arguments and "cart_id" in resources:
                                    arguments["cart_id"] = resources["cart_id"]
                                if op == "get_checkout" and "checkout_id" in resources:
                                    arguments["checkout_id"] = resources["checkout_id"]
                                mcp.headers["Idempotency-Key"] = "connect-" + str(index)
                                reply = await mcp.post(
                                    endpoint,
                                    json={
                                        "jsonrpc": "2.0",
                                        "id": index,
                                        "method": "tools/call",
                                        "params": {"name": op, "arguments": arguments},
                                    },
                                )
                                reply.raise_for_status()
                                result = reply.json()["result"]
                                if result.get("isError"):
                                    raise ValueError("MCP runtime check failed")
                                envelope = json.loads(result["content"][0]["text"])
                                if envelope.get("state") != "executed":
                                    raise ValueError("MCP action did not execute")
                                if op == "create_cart":
                                    resources["cart_id"] = envelope["result"]["id"]
                                if op == "create_checkout":
                                    resources["checkout_id"] = envelope["result"]["id"]
                                checks.append({"operation": op, "state": "executed"})
                            await mcp.delete(endpoint)
                            report["mcp"] = {"endpoint": endpoint, "tools": names, "checks": checks}
                            report["status"] = "locally_tested"
                    finally:
                        # Test credentials are revoked even if a check fails.
                        await call(prefix + "/mcp-credentials/" + granted["credential_id"], method="DELETE")
            report["credential_file"] = str(secret_path)
            report["connector_running"] = False
            return report
        except Exception as exc:
            report["status"] = "failed"
            report["error_type"] = type(exc).__name__
            raise
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await worker.close()
            await implementation.close()
            save(root, ".auteric/validation.json", report)


async def serve(root, credential_file):
    path = Path(credential_file).absolute()
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Connector credential must be a private regular file")
    settings = json.loads(path.read_text())
    config = json.loads(safe_path(root, ".auteric/connector.json").read_text())
    implementation, _, _ = connector(root, config, settings["development"])
    worker = EdgeWorker(
        implementation,
        api_url=settings["api_url"],
        store_id=settings["store_id"],
        token=settings["token"],
        database=str(runtime_directory() / (runtime_key(settings["api_url"], settings["store_id"]) + ".jobs.db")),
        environment=settings["environment"],
        allow_loopback=settings["development"],
    )
    try:
        await worker.run()
    finally:
        await worker.close()
        await implementation.close()


def main():
    data = json.load(sys.stdin)
    root = data["root"]
    if data["command"] == "inspect":
        result = inventory(root)
    elif data["command"] == "prepare":
        result = prepare(root, data.get("agent"), data.get("store_url"))
    elif data["command"] == "connect":
        result = asyncio.run(connect(root, data))
        save(root, ".auteric/validation.json", result)
    elif data["command"] == "serve":
        result = asyncio.run(serve(root, data["credential_file"]))
    else:
        raise ValueError("Unknown onboarding command")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
