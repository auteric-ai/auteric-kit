---
description: Prepare the current storefront for shopping agents with Auteric
---

Prepare this storefront for shopping agents with Auteric.

When the merchant asks to connect now, use the plugin-bundled CLI. For a local
store, its GitHub shortcut is `npx --yes github:auteric-ai/auteric-kit --localhost
--store-url http://127.0.0.1:5500`; flags without a subcommand mean `connect`.
It must scan and prove the backing commerce APIs before browser authorization,
Store creation, credentials, or UCP preparation. Never describe a failed or
incomplete scan as protected.

Start with a read-only inspection of the current repository: identify the framework, product catalog, cart, checkout and payment boundaries. Then explain the smallest safe integration plan in plain language, including files, routes, dependencies, tests, and any Auteric service requirements. Wait for approval before modifying application files. After approval, implement only what the existing storefront can support, run relevant checks, and clearly separate local completion from hosted Auteric setup still required.
