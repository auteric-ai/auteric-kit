---
name: auteric-connect
description: Connect a custom storefront project to Auteric by implementing catalog adapters, service-issued UCP discovery and scoped commerce controls. Use when the merchant asks to make their site accessible to shopping agents through Auteric.
---

# Connect a storefront

Work in the merchant's existing project. Identify its catalog source, backend routes, checkout flow and deployment target. Preserve the storefront design. Read [the workflow](references/workflow.md) and [the contract](references/contract.md) before implementing adapters or discovery. Show the merchant the proposed capabilities and exact backing routes before adding write actions.

Start with public catalog search and lookup. Map actual products, prices, currencies, availability, variants, images and canonical product URLs. Use existing business logic and authoritative inventory; never infer stock from a missing field. Keep pagination and variant identity stable. Retain purchase and payment on the merchant's existing flow unless a connected, tested integration supports more.

If the merchant has an Auteric service/account, use its authenticated store connection to obtain the discovery document and configured gateway URL. Publish the exact service-issued document at the merchant-controlled `/.well-known/ucp` route as JSON. Existing UCP content must be reconciled with the service; do not overwrite another integration blindly. A static host can publish the document, but cannot itself implement protected commerce operations.

If account credentials, a deployed gateway, or a signed discovery document are missing, complete the local catalog adapter and tests, prepare an explicit publication route, then report the exact missing connection dependency. Do not invent an SDK package installation, API URL, signing key, certificate or protected badge. This kit contains instructions and validation tooling, not a hosted gateway or credentials.

For configured actions, bind caller identity, tenant and resource ownership before dispatch. Enforce allowed operations and merchant policy, validate price/quantity on the server, and make writes idempotent. Add denial tests for unauthorized cross-store access and disallowed actions; do not collect payment credentials in public tools.

Test the real adapter with representative products, empty results, pagination, out-of-stock variants and malformed requests. Run `auteric-verify` after authorized publication. Report separately: local changes, public discovery, connected action evidence, and runtime enforcement. Agent discovery does not guarantee ranking, placement in a chat service, or additional purchases.
