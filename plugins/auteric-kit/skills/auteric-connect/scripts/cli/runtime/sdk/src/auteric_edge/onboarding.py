"""Local CLI bridge: inventory, install, exact contract tests and preparation.

JSON input is read from stdin so browser/connector credentials never enter argv.
No push, deployment, production write tests or public activation is performed.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .agent_setup import install
from .catalog import CatalogConnector, safe_path
from .manual import ManualConnector, load_factory
from .mapping import MappedConnector
from .mapping import mapping_digest
from .mapping import validate_mapping
from .models import validate_input, validate_output, READ_OPERATIONS
from .capability_registry import contract, registry_document
from .repository_inspection import inspect_repository
from .worker import EdgeWorker

OPERATION_ORDER = (
    "search_products", "get_product", "create_cart", "get_cart", "add_to_cart",
    "update_cart_item", "remove_from_cart", "replace_cart_items", "cancel_cart",
    "create_checkout", "get_checkout",
)


def ordered_mappings(mappings):
    rank = {operation: index for index, operation in enumerate(OPERATION_ORDER)}
    return sorted(mappings, key=lambda item: rank.get(item["operation"], len(rank)))


def lifecycle_test_input(operation, inputs, resources):
    value = dict(inputs[operation])
    if "cart_id" in value and resources.get("cart_id"):
        value["cart_id"] = resources["cart_id"]
    if "checkout_id" in value and resources.get("checkout_id"):
        value["checkout_id"] = resources["checkout_id"]
    return value


def remember_lifecycle_resource(operation, evidence, resources):
    response = evidence.get("response") or {}
    if operation == "create_cart" and response.get("id"):
        resources["cart_id"] = response["id"]
    if operation == "create_checkout" and response.get("id"):
        resources["checkout_id"] = response["id"]
    if operation == "cancel_cart":
        resources["cart_closed"] = True


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
    report["capability_contract_pool"] = registry_document()
    # A deterministic handoff for an Installation MCP or local coding agent:
    # discovery proposes bindings, but only a reviewed adapter may activate one.
    candidates = {}
    for item in report["candidates"]:
        candidates.setdefault(item["operation"], []).append({
            "method": item["behavior"].split(" ", 1)[0], "route": item["behavior"].split(" ", 1)[1],
            "source": item["source"], "line": item["line"], "confidence": item["confidence"],
        })
    report["installation_binding_plan"] = [
        {
            "operation": operation,
            "canonical_path": contract(operation).operation,
            "contract_version": "v1",
            "state": "requires_adapter_review",
            "merchant_candidates": entries,
        }
        for operation, entries in sorted(candidates.items())
    ]
    report["unsupported_runtime_domains"] = ["payment capture", "orders", "refunds", "identity linking"]
    return report


def _local_catalog_connector(root, report, store_url):
    from .catalog_probe import discover
    return discover(report, store_url)


def prepare(root, agent, store_url=None, instructions_root=None):
    report = inventory(root)
    destination = instructions_root or root
    clients = ("codex", "claude-code", "cursor") if agent == "auto" else (agent,)
    skill = []
    for client in clients:
        if client not in {"codex", "claude-code", "cursor"}:
            continue
        try:
            skill.append({"client": client, **install(client, destination)})
        except FileExistsError:
            skill.append({"client": client, "changed": False, "conflict": True})
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
        mappings = [{"operation": op, "kind": "sdk"} for op in sorted(implementation.operations)]
        inputs = {"search_products": {"query": "", "limit": 2}, "get_product": {"product_id": products[0]["id"]}}
    elif config["kind"] == "factory":
        # Explicit local module selection, never a remote job or inferred import.
        sys.path.insert(0, str(Path(root).absolute()))
        implementation = load_factory(config["factory"])
        mappings = [{"operation": op, "kind": "sdk"} for op in implementation.operations]
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


async def local_check(root, development):
    """Independent validation before authentication; no merchant writes."""
    config = json.loads(safe_path(root, '.auteric/connector.json').read_text())
    if config.get('kind') == 'rest':
        for mapping in config['mappings']:
            validate_mapping(mapping)
        digests = [mapping_digest(m) for m in config['mappings']]
        if not config.get('approved_mapping_digests'):
            config['approved_mapping_digests'] = digests
            save(root, '.auteric/connector.json', config)
        if set(config['approved_mapping_digests']) != set(digests):
            return {'status': 'mapping_changed', 'tested_operations': []}
    implementation, mappings, inputs = connector(root, config, development)
    checked = []
    try:
        if not mappings:
            return {'status': 'empty_connector', 'tested_operations': []}
        for item in mappings:
            operation = item['operation']
            if operation not in inputs:
                return {'status': 'test_inputs_required', 'operation': operation, 'tested_operations': checked}
            validate_input(operation, inputs[operation])
            if operation in READ_OPERATIONS and operation in {'search_products', 'get_product'}:
                result = await implementation.execute(operation, inputs[operation], {**item.get('mapping', item), 'operation': operation})
                validate_output(operation, result)
                checked.append(operation)
        return {'status': 'local_contract_passed', 'tested_operations': checked,
                'prepared_operations': [m['operation'] for m in mappings], 'merchant_writes_executed': False}
    finally:
        await implementation.close()


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
            contract_resources = {}
            versions_by_operation = {}
            for item in ordered_mappings(mappings):
                # The control plane receives a draft envelope. `item` is the
                # local deterministic REST/SDK mapping, while `operation` is
                # the canonical capability being activated.
                row = await call(prefix + "/mappings", {"operation": item["operation"], "mapping": item})
                versions_by_operation[item["operation"]] = row["id"]
                if item["operation"] == "create_checkout" and contract_resources.pop("cart_closed", False):
                    fresh = await call(
                        prefix + "/mappings/" + versions_by_operation["create_cart"] + "/test",
                        {"input": dict(inputs["create_cart"]), "mode": mode},
                    )
                    if fresh["status"] != "pass":
                        report["status"] = "contract_failed"
                        return report
                    remember_lifecycle_resource("create_cart", fresh, contract_resources)
                    if "add_to_cart" in versions_by_operation:
                        seeded = await call(
                            prefix + "/mappings/" + versions_by_operation["add_to_cart"] + "/test",
                            {"input": lifecycle_test_input("add_to_cart", inputs, contract_resources), "mode": mode},
                        )
                        if seeded["status"] != "pass":
                            report["status"] = "contract_failed"
                            return report
                test_input = lifecycle_test_input(item["operation"], inputs, contract_resources)
                evidence = await call(
                    prefix + "/mappings/" + row["id"] + "/test", {"input": test_input, "mode": mode}
                )
                report["mapping_versions"].append(
                    {"operation": item["operation"], "id": row["id"], "status": evidence["status"]}
                )
                if evidence["status"] != "pass":
                    report["status"] = "contract_failed"
                    return report
                report["tested_operations"].append(item["operation"])
                remember_lifecycle_resource(item["operation"], evidence, contract_resources)
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
                                        "clientInfo": {"name": "auteric-connect", "version": "0.6.0"},
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
                                if op == "cancel_cart" and "create_cart" in names:
                                    mcp.headers["Idempotency-Key"] = "connect-cancel-fixture"
                                    fixture = await mcp.post(
                                        endpoint,
                                        json={
                                            "jsonrpc": "2.0",
                                            "id": index + 1000,
                                            "method": "tools/call",
                                            "params": {"name": "create_cart", "arguments": {**inputs["create_cart"], "items": []}},
                                        },
                                    )
                                    fixture.raise_for_status()
                                    fixture_result = fixture.json()["result"]
                                    if fixture_result.get("isError"):
                                        raise ValueError("MCP cancel fixture could not create an isolated cart")
                                    fixture_envelope = json.loads(fixture_result["content"][0]["text"])
                                    if fixture_envelope.get("state") != "executed":
                                        raise ValueError("MCP cancel fixture did not execute")
                                    resources["cart_id"] = fixture_envelope["result"]["id"]
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
                            defaults = [
                                {"operation": operation, "enabled": operation in {"search_products", "get_product"}}
                                for operation in report["tested_operations"]
                            ]
                            report["capability_defaults"] = await call(
                                prefix + "/capabilities", {"capabilities": defaults}, method="PUT"
                            )
                            # Changing the exposed capability set intentionally
                            # invalidates the prior activation evidence. Re-test
                            # the safe default surface, then restore agent access
                            # against that exact binding. All other mappings stay
                            # tested and connected for later dashboard activation.
                            report["all_capabilities_connection_test"] = report["connection_test"]
                            report["connection_test"] = await call(
                                prefix + "/test-transaction",
                                {"product_id": inputs.get("get_product", {}).get("product_id"), "query": ""},
                            )
                            if report["connection_test"]["state"] != "passed":
                                raise ValueError("Default catalog capability test failed")
                            await call(prefix + "/agent-access", {"enabled": True}, method="PUT")
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
    health = {'status': 'starting', 'store_id': settings['store_id'], 'pid': os.getpid(), 'last_success': None}
    try:
        while True:
            try:
                busy = await worker.tick()
                health.update(status='healthy', last_success=time.time(), failures=0)
            except httpx.HTTPStatusError as exc:
                health.update(status='authorization_failed' if exc.response.status_code in {401, 403} else 'unavailable', failures=health.get('failures', 0) + 1)
                if exc.response.status_code in {401, 403}:
                    save(root, '.auteric/health.json', health)
                    raise
                busy = False
            except (httpx.HTTPError, ValueError):
                health.update(status='unavailable', failures=health.get('failures', 0) + 1)
                busy = False
            health['checked_at'] = time.time()
            save(root, '.auteric/health.json', health)
            await asyncio.sleep(0.1 if busy else min(30, 2 ** min(health.get('failures', 0) + 1, 5)))
    finally:
        await worker.close()
        await implementation.close()


def main():
    data = json.load(sys.stdin)
    root = data["root"]
    if data["command"] == "inspect":
        result = inventory(root)
    elif data["command"] == "prepare":
        result = prepare(root, data.get("agent"), data.get("store_url"), data.get("instructions_root"))
    elif data["command"] == "connect":
        result = asyncio.run(connect(root, data))
        save(root, ".auteric/validation.json", result)
    elif data['command'] == 'local-check':
        try:
            result = asyncio.run(local_check(root, data.get('development', False)))
        except Exception as exc:
            result = {'status': 'local_contract_failed', 'error_type': type(exc).__name__, 'tested_operations': []}
        save(root, '.auteric/local-validation.json', result)
    elif data["command"] == "serve":
        result = asyncio.run(serve(root, data["credential_file"]))
    else:
        raise ValueError("Unknown onboarding command")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
