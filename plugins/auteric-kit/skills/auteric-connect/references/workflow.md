# Custom storefront implementation workflow

Follow these steps in the merchant's repository. Keep source review local. Never send application code, customer data, or secrets to a public scan.

## 1. Inventory and plan

Identify the actual framework, deployment method, product source, inventory authority, cart state, checkout handoff, authentication and existing UCP/WebMCP interfaces. Record exact files and routes. Propose a small implementation table with each intended capability, its backing store function, side effects, auth boundary, proposed files and proof test. Mark unknown data as unknown. Do not infer that an existing button provides a safe agent API.

For public catalog work, a read-only implementation can proceed in the store repository. Any write action needs a merchant-reviewed scope and a server-side authorization path. Existing UCP must be merged deliberately with the owner; avoid replacing unrelated declarations.

## 2. Implement catalog reads first

Use the store's own read model. Product search and lookup must preserve stable product/variant IDs and canonical URLs. Return current title, price, currency and availability from the authoritative source; distinguish an unavailable value from zero or out of stock. Bound page size and input length. Encode amounts in correct minor units at the UCP boundary. Do not publish private inventory counts, customer records, admin URLs, access tokens or internal errors.

Test at least: representative in-stock product, out-of-stock variant, pagination boundary, no matches, unknown ID, missing optional fields, invalid query, and price/currency conversion. If product data is only public HTML, report catalog discovery as sampled public evidence, not an authoritative merchant feed.

## 3. Connect to the service

Only a configured HTTPS Auteric Commerce service can create an authenticated Store, connector credential, Gateway route and signed discovery document. The authorized merchant/operator provisions it; do not bootstrap from a guessed URL or a key embedded in public JSON. For current service contracts, see `contract.md`. Keep one-time connector credentials in the store's secret manager, never in source control or agent-visible output. The connection needs a current connector heartbeat, tested mappings, enabled capabilities, policy and merchant-domain binding before production agent traffic is considered ready.

If no live service or merchant account is available, stop at prepared local code. State the exact blocker and do not produce a fake UCP signature or protected badge.

## 4. Publish and verify

Publish the exact current service-issued document at `https://<merchant-host>/.well-known/ucp` with HTTP 200 and JSON content type. Prefer the store's existing deployment pipeline. Keep checkout and payment at the merchant's existing site. Re-run the kit's read-only `check_connection.py` with an independently trusted key, then the Scanner onboarding connection check.

Record four independent states: (a) merchant code tested locally, (b) public catalog/UCP observed on HTTPS, (c) signed Auteric exposure verified, (d) connected action and runtime protection verified. Do not collapse them into one “connected” label. A blocked/Cloudflare response is blocked evidence, not an absent capability and never a reason to bypass controls.

## 5. Review result

Hand the merchant a short table: capability, actual backing route, tests, deployed evidence, and remaining dependency. Include exact scan ID/timestamp and Gateway health record only if observed. Do not promise ChatGPT placement, agent sales, or protection merely because discovery is published.
