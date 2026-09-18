"""Local tools; no package registry release or real merchant mutation by default."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from .connector import MockConnector
from .manual import load_factory
from .mapping import MappedConnector, suggest_mappings, validate_mapping
from .worker import EdgeWorker


async def self_test():
    connector = MockConnector()
    products = await connector.execute("search_products", {"query": "shoes"})
    product = await connector.execute("get_product", {"product_id": products[0]["id"]})
    cart = await connector.execute("create_cart", {})
    cart = await connector.execute("add_to_cart", {"cart_id": cart["id"], "product_id": product["id"], "quantity": 2})
    cart = await connector.execute(
        "update_cart_item", {"cart_id": cart["id"], "product_id": product["id"], "quantity": 1}
    )
    assert (await connector.execute("get_cart", {"cart_id": cart["id"]}))["total"] == "100.00"
    checkout = await connector.execute("create_checkout", {"cart_id": cart["id"]})
    assert (await connector.execute("get_checkout", {"checkout_id": checkout["id"]}))["cart_id"] == cart["id"]
    cart = await connector.execute("remove_from_cart", {"cart_id": cart["id"], "product_id": product["id"]})
    assert not cart["items"]
    print("PASS: all 9 canonical operations against the in-memory mock; no payment or real merchant calls")


def main():
    parser = argparse.ArgumentParser(prog="auteric-commerce")
    commands = parser.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("agent", help="Local coding-agent instruction package")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("instructions", help="Print the complete read-only-first integration workflow")
    installer = agent_commands.add_parser("install", help="Install instructions only; never overwrite existing files")
    installer.add_argument("--client", choices=["codex", "claude-code", "cursor"], required=True)
    installer.add_argument("--destination", required=True, help="Explicit existing merchant repository")
    packager = agent_commands.add_parser("package", help="Build a local plugin without installing or publishing")
    packager.add_argument("--destination", required=True, help="Existing output parent; plugin folder must not exist")
    plan = agent_commands.add_parser("check-plan", help="Validate a review artifact; never approve or execute")
    plan.add_argument("file")
    plan.add_argument("--approval")
    plan.add_argument("--report")
    validate = commands.add_parser("validate")
    validate.add_argument("file", nargs="?")
    commands.add_parser("test")
    setup = commands.add_parser("setup", help="Authenticate a coding-assisted custom Store setup")
    setup.add_argument("--api-url", help="Optional override; normally encoded by the short-lived authorization")
    setup.add_argument(
        "--authorization",
        default=os.environ.get("AUTERIC_SETUP_AUTHORIZATION"),
        help="Short-lived Store setup authorization (or AUTERIC_SETUP_AUTHORIZATION)",
    )
    setup.add_argument("--mappings", help="Reviewed JSON file containing a mappings array to submit as drafts")
    setup.add_argument("--complete", action="store_true", help="Revoke the setup authorization after submission")
    setup.add_argument("--project-root", help="Inspect this local repository read-only; emit review candidates, never apply or submit them")
    setup.add_argument("--publication-framework", choices=['express', 'next', 'vite', 'fastapi', 'flask'], help="Emit review-only publication route files for the authenticated Store")
    setup.add_argument("--adapter-recipe", help="Reviewed local Python service bindings JSON; requires --project-root; emits code but never applies it")
    setup.add_argument("--development", action="store_true", help="Permit an explicit loopback HTTP API URL")
    connector = commands.add_parser("connector")
    connector.add_argument("action", choices=["start", "verify"])
    connector.add_argument("--mock", action="store_true")
    connector.add_argument("--config", help="Local JSON: base_url, allowed_paths, credential_env list")
    connector.add_argument("--factory", help="Trusted installed local module:factory returning CommerceConnector")
    connector.add_argument("--shopify-installation", action="store_true", help="Use the Shopify installation bound to AUTERIC_STORE_ID")
    connector.add_argument(
        "--platform-config", help="Reviewed local native platform JSON; secret environment references only"
    )
    connector.add_argument(
        "--check", action="store_true", help="Check local integration shape without polling or merchant operations"
    )
    connector.add_argument("--development", action="store_true", help="Permit loopback HTTP only")
    connector.add_argument(
        "--environment",
        choices=["sandbox", "staging", "production"],
        default="sandbox",
        help="Must match the store environment in the control plane",
    )
    connector.add_argument("--query", default="", help="Read-only catalog acceptance query")
    connector.add_argument("--product-id", help="Explicit safe sandbox product for write acceptance")
    connector.add_argument("--variant-id", help="Explicit safe sandbox variant, when required")
    connector.add_argument("--quantity", type=int, default=1)
    connector.add_argument(
        "--allow-sandbox-writes",
        action="store_true",
        help="Permit a bounded sandbox cart/checkout-handoff journey; never payment",
    )
    args = parser.parse_args()
    if args.command == "agent":
        from .agent_setup import install, instructions, package

        if args.agent_command == "instructions":
            print(instructions())
        elif args.agent_command == "package":
            print(json.dumps(package(args.destination), indent=2))
        elif args.agent_command == "check-plan":
            from .integration_plan import Approval, IntegrationPlan, ValidationReport, plan_digest, verify_review

            artifact = IntegrationPlan.model_validate_json(Path(args.file).read_text())
            if args.report and not args.approval:
                parser.error("--report requires --approval; no approval is inferred")
            if args.approval:
                approval = Approval.model_validate_json(Path(args.approval).read_text())
                report = ValidationReport.model_validate_json(Path(args.report).read_text()) if args.report else None
                print(json.dumps(verify_review(artifact, approval, report), indent=2))
            else:
                print(
                    json.dumps(
                        {"plan_digest": plan_digest(artifact), "approval": "not_checked", "production_ready": False}
                    )
                )
        else:
            print(json.dumps(install(args.client, args.destination), indent=2))
    elif args.command == "validate":
        if args.file:
            data = json.loads(Path(args.file).read_text())
            result = suggest_mappings(data) if "openapi" in data or "swagger" in data else validate_mapping(data)
            print(json.dumps(result, indent=2))
        else:
            from .models import INPUTS, OUTPUTS

            for operation, model in INPUTS.items():
                model.model_json_schema()
                OUTPUTS[operation].json_schema()
            print("PASS: canonical schema definitions validated")
    elif args.command == "test":
        asyncio.run(self_test())
    elif args.command == "setup":
        if not args.authorization:
            parser.error("Provide --authorization or AUTERIC_SETUP_AUTHORIZATION")

        async def run_setup():
            from .setup_client import SetupClient

            async with SetupClient(
                args.api_url, args.authorization, development=args.development
            ) as setup_client:
                result = {"context": await setup_client.context(), "mappings": None, "completed": False}
                if args.project_root:
                    from .repository_inspection import inspect_repository
                    result['repository_review'] = inspect_repository(args.project_root)
                if args.publication_framework:
                    from .publication import publication_proposal
                    result['publication_review'] = publication_proposal(args.publication_framework, result['context']['managed_profile_url'])
                if args.adapter_recipe:
                    from .adapter_generation import generate_adapter
                    if not args.project_root:
                        raise ValueError('--adapter-recipe requires the explicit merchant --project-root')
                    result['adapter_review'] = generate_adapter(args.project_root, json.loads(Path(args.adapter_recipe).read_text(encoding='utf-8')))
                if args.mappings:
                    document = json.loads(Path(args.mappings).read_text(encoding="utf-8"))
                    mappings = document.get("mappings") if isinstance(document, dict) else None
                    if not isinstance(mappings, list) or not mappings:
                        raise ValueError("Mappings file must contain a non-empty mappings array")
                    result["mappings"] = await setup_client.propose_mappings(mappings)
                if args.complete:
                    result["completion"] = await setup_client.complete()
                    result["completed"] = True
                print(json.dumps(result, indent=2))

        asyncio.run(run_setup())
    else:
        if sum(bool(value) for value in (args.mock, args.config, args.factory, args.platform_config, args.shopify_installation)) != 1:
            parser.error("Choose exactly one of --mock, --config, --factory, --platform-config or --shopify-installation")
        if args.mock and args.environment == "production":
            parser.error("Mock connectors cannot register as production")
        if args.development and args.environment == "production":
            parser.error("Production connectors cannot enable loopback HTTP development mode")

        async def start():
            if args.shopify_installation:
                from .platforms.shopify_installation import installed_connector

                implementation = await installed_connector(api_url=os.environ['AUTERIC_API_URL'],
                    store_id=os.environ['AUTERIC_STORE_ID'], token=os.environ['AUTERIC_CONNECTOR_TOKEN'],
                    database=os.environ.get('AUTERIC_SHOPIFY_CART_DATABASE', '.runtime/shopify-carts.db'), development=args.development)
            elif args.mock:
                implementation = MockConnector()
            elif args.factory:
                implementation = load_factory(args.factory)
            elif args.platform_config:
                from .manual import ManualConnector
                from .platforms.loader import load_platform

                implementation = ManualConnector(load_platform(json.loads(Path(args.platform_config).read_text())))
            else:
                config = json.loads(Path(args.config).read_text())
                credentials = {key: os.environ[key] for key in config.get("credential_env", [])}
                if not args.development and not config.get("approved_mapping_digests"):
                    raise ValueError("Production connectors require locally pinned approved_mapping_digests")
                implementation = MappedConnector(
                    config["base_url"],
                    allowed_paths=config["allowed_paths"],
                    credentials=credentials,
                    approved_mapping_digests=config.get("approved_mapping_digests"),
                    allow_loopback=args.development,
                )
            if args.action == "verify":
                if args.config:
                    raise ValueError("Native acceptance requires --platform-config, --factory or --mock")
                from .acceptance import OPERATIONS, run_acceptance

                target = getattr(implementation, "implementation", implementation)
                if args.mock:
                    target.supported_operations = frozenset(OPERATIONS)
                report = await run_acceptance(
                    target,
                    environment=args.environment,
                    query=args.query,
                    product_id=args.product_id,
                    variant_id=args.variant_id,
                    quantity=args.quantity,
                    allow_sandbox_writes=args.allow_sandbox_writes,
                )
                print(json.dumps(report, indent=2))
                if hasattr(implementation, "close"):
                    await implementation.close()
                return
            if args.check:
                print("Local integration loaded; no merchant operations or control-plane registration performed.")
                if hasattr(implementation, "operations"):
                    print("Implemented operations: " + ", ".join(sorted(implementation.operations)))
                if hasattr(implementation, "close"):
                    await implementation.close()
                return
            worker = EdgeWorker(
                implementation,
                api_url=os.environ["AUTERIC_API_URL"],
                store_id=os.environ["AUTERIC_STORE_ID"],
                token=os.environ["AUTERIC_CONNECTOR_TOKEN"],
                release_digest=os.environ.get("AUTERIC_CONNECTOR_RELEASE_SHA256"),
                database=os.environ.get("AUTERIC_JOB_DATABASE", ".runtime/jobs.db"),
                environment=args.environment,
                allow_loopback=args.development,
            )
            print("Connector running outbound; no inbound firewall rule required. Ctrl-C to stop.")
            try:
                await worker.run()
            finally:
                await worker.close()
                if hasattr(implementation, "close"):
                    await implementation.close()

        asyncio.run(start())


if __name__ == "__main__":
    main()
