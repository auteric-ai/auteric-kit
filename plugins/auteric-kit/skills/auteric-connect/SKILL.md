---
name: auteric-connect
description: Prepare an ecommerce or storefront project for shopping agents with Auteric. Use when asked to make a store agent-ready, integrate agent commerce, expose catalog/cart/checkout to commerce agents, prepare shopping access, set up Auteric, or connect a storefront to AI shopping. Do not use for unrelated work in an ecommerce repository.
---

# Prepare a storefront for shopping agents

If the user asks for a single-command setup, run
`npx --yes github:auteric-ai/auteric-kit --domain STORE` or
`npx --yes github:auteric-ai/auteric-kit --localhost --store-url http://127.0.0.1:5500 --serve`
from the merchant repository root. Local mode requires the storefront and Auteric
Commerce service to be running. The CLI supports local HTTP control-plane
and storefront verification. The CLI installs the bundled SDK and project skill, inventories all canonical
capabilities, installs supported local adapters, performs browser authorization
and exact sandbox contract/Gateway tests, then prepares signed UCP locally.
`--serve` keeps the connector running in that terminal after verification. Inspect
`.auteric/capabilities.json` and `.auteric/validation.json` for missing operations. Do not claim `npx @auteric/cli` is
available or that local development signatures prove public protection.

Start at the merchant repository root. The CLI searches for a frontend and API
service beneath it. If it finds one of each, it chooses them automatically. If it
finds several API services, it stops before sign-in and asks for the authoritative
commerce service through `--backend <relative-directory>`; `--frontend` selects
the deployment project that will publish `/.well-known/ucp`. The backend is where
the connector and capability reports belong. A browser-only frontend is never
treated as authority for cart, checkout, inventory, price, or payment.

This is a guided workflow. The merchant does not need to know this skill's name.
Read [the workflow](references/workflow.md), [the implementation guide](references/implementation.md), and [the contract](references/contract.md) before proposing changes.
For a custom API integration or an empty UCP, also read [capability completion](references/capability-completion.md). A CLI invocation alone cannot implement arbitrary merchant business logic.

## 1. Inspect — read-only

Work in the merchant's existing project. Identify the framework/runtime, package manager, deployment route, catalog and inventory source, product/variant model, cart flow, checkout/payment boundary, authentication boundary, and existing UCP or agent-facing interfaces. Record exact source files and routes. Preserve the storefront design and existing payment system.

Do not write files, install dependencies, create a branch, call a hosted Auteric service, or create credentials during this phase.

## 2. Explain the local changes and continue

Present a short plan in business language before any material modification. It must include: files to create or modify, dependencies to add, read-only catalog capabilities, any cart/checkout handoff proposed, routes/configuration to add, local validation, and the external Auteric account/service information still required. Mark each capability as **available from existing code**, **requires merchant decision**, or **requires Auteric service**.

A request to connect or implement authorizes local installation, dependencies,
browser sign-in, adapters and local/sandbox tests. Continue those steps without
another approval prompt. Inspection-only requests remain read-only. Browser login
and pairing must still be completed by the real account owner.

Wait for normal user approval before pushing code, publishing packages, deploying
merchant discovery or enabling production traffic. Prepare and validate the exact
changes before this publication gate; local installation is not publication.

## 3. Implement all supported local capabilities

Implement the smallest integration that the inspected project can honestly support. Start with catalog search and product lookup, using the store's authoritative product, price, currency, availability, variant, image, and canonical URL data. Add server-side validation, bounded pagination, stable product identity, and tests appropriate to the project's stack.

Reuse the existing cart and checkout flow. Never replace payment logic, collect payment credentials, expose secrets client-side, or declare a write action merely because a storefront button exists. For any approved write capability, bind caller identity and merchant/resource ownership on the server, validate price and quantity again, make writes idempotent, and add an allow/deny test.

If the merchant supplies a configured Auteric service and credentials, integrate only its documented values. Publish the exact service-issued JSON at the merchant-controlled `/.well-known/ucp` route. Merge an existing UCP document deliberately; never overwrite another integration. If service access is absent, finish all supportable local work and label the remote step as pending.

## 4. Verify and report

Run the project's relevant tests, typecheck, build, or lint commands when available, then run the bundled public verifier after authorized publication. Fix integration-caused failures before reporting completion.

End with these explicit sections: **Completed locally**, **Requires Auteric credentials/service**, **Requires merchant/operator action**, and **Validation results**. List created and modified files, dependencies, routes, catalog/cart/checkout status, and exact unverified boundaries. A local test, copied prompt, or public declaration is never a claim of live protection or AI-platform placement.

Use the SDK capability report to cover search_products, get_product, create_cart,
get_cart, add_to_cart, update_cart_item, remove_from_cart, replace_cart_items,
cancel_cart, create_checkout and get_checkout. Trace every candidate to its actual
business logic. Complete factory/REST adapters and sandbox test inputs where
supported; do not stop after catalog if real cart or checkout APIs exist.
Unsupported and untested operations must have explicit reasons. Payments, refunds,
orders and identity linking are not supported by this canonical runtime and must
not be invented. Local skill installation is separate from runtime tool exposure.
A shared MCP process serves logical Store endpoints and filters tools by active
mappings, capability controls and store-scoped grants. Read its signed
`auteric_mcp.endpoint`; do not guess a new per-merchant server.
