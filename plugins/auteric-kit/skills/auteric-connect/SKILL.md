---
name: auteric-connect
description: Prepare an ecommerce or storefront project for shopping agents with Auteric. Use when asked to make a store agent-ready, integrate agent commerce, expose catalog/cart/checkout to commerce agents, prepare shopping access, set up Auteric, or connect a storefront to AI shopping. Do not use for unrelated work in an ecommerce repository.
---

# Prepare a storefront for shopping agents

This is a guided workflow. The merchant does not need to know this skill's name.
Read [the workflow](references/workflow.md), [the implementation guide](references/implementation.md), and [the contract](references/contract.md) before proposing changes.

## 1. Inspect — read-only

Work in the merchant's existing project. Identify the framework/runtime, package manager, deployment route, catalog and inventory source, product/variant model, cart flow, checkout/payment boundary, authentication boundary, and existing UCP or agent-facing interfaces. Record exact source files and routes. Preserve the storefront design and existing payment system.

Do not write files, install dependencies, create a branch, call a hosted Auteric service, or create credentials during this phase.

## 2. Explain the plan — required approval gate

Present a short plan in business language before any material modification. It must include: files to create or modify, dependencies to add, read-only catalog capabilities, any cart/checkout handoff proposed, routes/configuration to add, local validation, and the external Auteric account/service information still required. Mark each capability as **available from existing code**, **requires merchant decision**, or **requires Auteric service**.

Wait for normal user approval. Do not treat installation of this plugin, a starter prompt, or a request to inspect as approval to modify application code. If the plan changes materially, present the revised plan and wait again.

## 3. Implement — only after approval

Implement the smallest integration that the inspected project can honestly support. Start with catalog search and product lookup, using the store's authoritative product, price, currency, availability, variant, image, and canonical URL data. Add server-side validation, bounded pagination, stable product identity, and tests appropriate to the project's stack.

Reuse the existing cart and checkout flow. Never replace payment logic, collect payment credentials, expose secrets client-side, or declare a write action merely because a storefront button exists. For any approved write capability, bind caller identity and merchant/resource ownership on the server, validate price and quantity again, make writes idempotent, and add an allow/deny test.

If the merchant supplies a configured Auteric service and credentials, integrate only its documented values. Publish the exact service-issued JSON at the merchant-controlled `/.well-known/ucp` route. Merge an existing UCP document deliberately; never overwrite another integration. If service access is absent, finish all supportable local work and label the remote step as pending.

## 4. Verify and report

Run the project's relevant tests, typecheck, build, or lint commands when available, then run the bundled public verifier after authorized publication. Fix integration-caused failures before reporting completion.

End with these explicit sections: **Completed locally**, **Requires Auteric credentials/service**, **Requires merchant/operator action**, and **Validation results**. List created and modified files, dependencies, routes, catalog/cart/checkout status, and exact unverified boundaries. A local test, copied prompt, or public declaration is never a claim of live protection or AI-platform placement.
