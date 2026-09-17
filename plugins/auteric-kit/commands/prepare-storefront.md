---
description: Prepare the current storefront for shopping agents with Auteric
---

Prepare this storefront for shopping agents with Auteric.

For a single-command local control-plane sign-in, the unpublished kit CLI can
run `node /path/to/auteric-kit/bin/auteric.js connect --domain STORE --localhost --store-url http://127.0.0.1:5500`.
This currently registers a pending store after browser authorization; it does
not install adapters or prove shopping actions. Never describe it as protected.

Start with a read-only inspection of the current repository: identify the framework, product catalog, cart, checkout and payment boundaries. Then explain the smallest safe integration plan in plain language, including files, routes, dependencies, tests, and any Auteric service requirements. Wait for approval before modifying application files. After approval, implement only what the existing storefront can support, run relevant checks, and clearly separate local completion from hosted Auteric setup still required.
