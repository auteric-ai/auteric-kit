# Complete the integration before producing discovery

An authenticated account, Store record, dashboard or signed empty profile is not a
working commerce connection. When Connect returns `implementation_required`, the
coding agent still has work to do: read the backend, implement a local factory or
REST connector, supply sandbox test inputs and rerun Connect. Do not ask the merchant
to repeat the same command until its missing implementation has been addressed.

The bounded CLI inspector does not scan every possible language or route wrapper.
Trace custom registration helpers, computed route prefixes, OpenAPI, GraphQL and
client calls manually when the automatic inventory misses them. Do not mark an API
absent because a pattern did not match. Browser state may coexist with a real backend.

Record a coverage table for catalog, cart, checkout, fulfillment, discounts, orders,
payments and identity. For each, record source route/function, authorization, adapter
operation, test result and publication status. Distinguish:

- implemented and tested through Auteric;
- present in merchant code but needs an adapter;
- present in merchant code but unsupported by the current Auteric protocol runtime;
- sandbox-only or not present.

The current runtime supports eleven canonical operations, not every UCP domain.
Do not stop after catalog if safe cart APIs can be mapped, but do not claim order,
refund, discount, fulfillment or payment protocol support merely because the store
has routes for them. Explain platform gaps as platform gaps.

Use a fixed local factory when response transformation, variant/line identity or
resource lifecycle cannot be represented by the REST mapping language. Preserve
server-side prices, owner/session isolation and idempotency. Do not share one shopper
session across unrelated callers or simulate atomic cart replacement with unguarded
delete/add calls. Checkout completion is different from preparing a handoff URL;
a route that debits stock or creates a paid order is not `create_checkout`.

UCP is a discovery contract, not the product database. Enrich only with verified
capabilities, working schemas/endpoints and supported configuration. Older versions
need real versioned routes; Google Pay and Shop Pay need real handler configuration;
Shopify extensions belong only to implementations that support them. Auteric's
canonical MCP endpoint is not the official UCP MCP binding. Product descriptions,
prices, variants and stock belong in catalog responses.

After tests, fetch a fresh service-issued profile, validate its declared capabilities
against active operations, and verify the actual storefront route. Keep the previous
profile intact on failure and clearly report it as stale. Never manually edit a
signed payload or present a local sandbox signature as production trust.
