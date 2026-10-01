---
name: auteric-connect
description: Prepare an ecommerce or storefront project for shopping agents with Auteric. Use when asked to make a store agent-ready, integrate agent commerce, expose catalog/cart/checkout to commerce agents, prepare shopping access, set up Auteric, or connect a storefront to AI shopping. Do not use for unrelated work in an ecommerce repository.
---

# Prepare a storefront for shopping agents

Custom Connect defaults to a model-generated adapter and one generic, versioned
Auteric runtime container. Read [model-driven Connect](references/model-connect.md)
first. The coding model already working in the merchant repository scans and
interprets it, creates the adapter and fixtures, and repairs only that integration.
Inventory assists this work; it cannot prove business semantics. Do not start a
second coding agent. No SDK source or runtime environment is copied into the merchant.

Use [managed HTTP Connect](references/managed-http.md) only for an explicitly
provided simple HTTP mapping. Local acceptance must precede cloud qualification.
The native and old installer paths below are compatibility paths, selected
explicitly. They do not override the default out-of-process model.

## One Connect request

For custom stores the user supplies `auteric connect --domain STORE` to the
current frontier coding model. Complete the entire local workflow in this same
request. Read `references/model-connect.md`; run the source CLI, inspect its
dossier and merchant code, generate the owned plan under ignored
`auteric/.state/adapter-plan.json`, and resume the same command internally.
Do not stop at `implementation_required`, ask for `--adapter-plan`, or hand a
second command back to the user. Only stop for a concrete unresolved merchant
semantic boundary or unavailable local prerequisite. Never invent a successful
binding. A terminal without a coding model returns a continuation for that model;
the CLI itself does not contain repository-specific intelligence.
Connect installs the plan automatically and runs local acceptance by default.
Runtime source/baseline options, when needed, are internal model setup details.
Prepare discovery routing from actual verified capabilities and report local
verification separately from public deployment. Do not use the legacy installer
for a single-command request.

Start at the merchant repository root. The CLI searches for a frontend and API
service beneath it. If it finds one of each, it chooses them automatically. If it
finds several API services, it stops before sign-in and asks for the authoritative
commerce service through `--backend <relative-directory>`; `--frontend` selects
the deployment project that will publish `/.well-known/ucp`. The backend is where
the connector and capability reports belong. A browser-only frontend is never
treated as authority for cart, checkout, inventory, price, or payment.

This is a guided workflow. The merchant does not need to know this skill's name.
Read [the workflow](references/workflow.md), [the implementation guide](references/implementation.md), and [the contract](references/contract.md) before proposing changes.
For a custom API integration or an empty UCP, also read [capability completion](references/capability-completion.md). Connect can invoke an available coding CLI to prepare a missing adapter; this is
a bounded implementation attempt followed by independent validation, not universal
API compatibility. Inside a coding assistant, pass `--no-agent`, implement the
adapter in the current task, and rerun Connect. Never nest Connect when
`AUTERIC_AGENT_TASK=1`. Use `--backend-url` when the API and storefront origins differ.
Read `.auteric/workflow.json` for the last stage, `.auteric/local-validation.json`
for pre-authentication checks and `.auteric/health.json` for worker connectivity.
A resumed session does not prove current runtime protection.

## Installation SDK vs merchant runtime SDK

Two different SDKs are involved, and they are not interchangeable:

- The **installation-time SDK** is this CLI and its code generator
  (`inventory` → `bind` → `validate` → `verify`). It reads the merchant
  repository, generates adapters and a composition root, validates them
  statically, and runs the contract acceptance suite in dev mode. It never
  serves shopper traffic.
- The **merchant runtime SDK** runs inside the merchant backend and serves the
  Merchant Execution Protocol (MEP/1): `@auteric/merchant-node` (Node),
  `auteric-merchant` (Python), `merchant-go` (Go). The binding planner selects
  it from the authoritative backend's language: `native_node`,
  `native_python`, or `native_go`.

For the explicit native/legacy fallback only, choose the runtime per ADR-01 conditions: a backend that can receive HTTPS
gets the native runtime in its own language; `outbound_worker` is chosen only
on deployment constraints (private network, no ingress); a static-only
storefront gets a limited read-only catalog or an explicit companion backend —
never infer a backend from Vite or static hosting. See
[the runtime matrix](../../../../docs/runtime-matrix.md) for per-framework support
levels and what is fixture-tested versus generally available.

## 1. Inspect — read-only

Work in the merchant's existing project. Identify the framework/runtime, package manager, deployment route, catalog and inventory source, product/variant model, cart flow, checkout/payment boundary, authentication boundary, and existing UCP or agent-facing interfaces. Record exact source files and routes. Preserve the storefront design and existing payment system.

Keep every detected API in `api_inventory`, including infrastructure, identity,
admin, payment and unrelated routes. Generate connector mappings only for entries
marked `tool_eligible` with a supported canonical shopping operation. Entries marked
`inventory_only`, `internal_dependency` or `blocked_by_policy` provide context and
must never become MCP tools.

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
For Express/static production previews, verify the actual running server serves
that route from the current signed file with `Content-Type: application/json`;
an SPA fallback or extensionless octet-stream response is not valid discovery.

## 4. Verify and report

Run the project's relevant tests, typecheck, build, or lint commands when available, then run the bundled public verifier after authorized publication. Fix integration-caused failures before reporting completion.

End with these explicit sections: **Completed locally**, **Requires Auteric credentials/service**, **Requires merchant/operator action**, and **Validation results**. List created and modified files, dependencies, routes, catalog/cart/checkout status, and exact unverified boundaries. A local test, copied prompt, or public declaration is never a claim of live protection or AI-platform placement.

Use the SDK capability report to cover every operation the locked contracts
registry defines (the list below is generated from the registry; treat the
registry, not this document, as the source of truth for the current count):

<!-- auteric:capabilities:start -->
<!-- Generated from packages/commerce-contracts/registry/operations (locked contracts registry) by kits/auteric-kit/tools/skill_capability_summary.js. Do not edit by hand; rerun the tool. -->
The locked contracts registry defines **20 canonical operations** in 6 families:
- cart (7): add_to_cart, cancel_cart, create_cart, get_cart, remove_from_cart, replace_cart_items, update_cart_item
- catalog (2): get_product, search_products
- checkout (5): cancel_checkout, complete_checkout, create_checkout, get_checkout, update_checkout
- discount (2): apply_discount_code, remove_discount_code
- orders (1): get_order
- shipping (3): get_shipping_options, select_shipping_option, set_shipping_address
<!-- auteric:capabilities:end -->

Trace every candidate to its actual
business logic. Complete factory/REST adapters and sandbox test inputs where
supported; do not stop after catalog if real cart or checkout APIs exist.
Unsupported and untested operations must have explicit reasons. Checkout completion
requires a real merchant payment handler, encrypted sensitive-job transport,
idempotency and an order-confirmation adapter; never infer those from a payment
route or frontend button. Refunds and identity linking must not be invented. Local
skill installation is separate from runtime tool exposure.
A shared MCP process serves logical Store endpoints and filters tools by active
mappings, capability controls and store-scoped grants. Read its signed
`auteric_mcp.endpoint`; do not guess a new per-merchant server.

There are two different MCP roles. Gateway MCP is the always-available shopper
runtime, with a Store-scoped endpoint and only tested, active mapped capabilities.
Integration MCP is an optional developer installation surface; this skill and CLI
can perform the same installation without it. Never advertise installation tools
as shopper tools or require Integration MCP to keep Gateway MCP working. Connect
automatically provisions a private, read-only Gateway grant after successful local
discovery verification (or tested cloud integration), and stores it outside the
merchant repository. The connector process must remain running for actual tool
calls. The pairing value shown as Merchant ID is an expiring 8-digit code tied to
the signed-in account, not a permanent Store ID or proof of domain ownership.
