# Auteric connection contract

This snapshot matches the Auteric commerce service contract as of 2026-09-16. Confirm the configured service version before changing an existing connection. The package is standalone; the source pointers below are maintenance references, not required imports.

## Catalog adapter

Canonical product fields: `id`, `sku`, `title`, decimal `price`, three-letter uppercase `currency`; optional `description`, `inventory`, `variants`, `images`, `product_url`. Availability is one of `in_stock`, `out_of_stock`, `preorder`, `unknown`. IDs and variant IDs must persist across scans. Return canonical HTTPS product/image URLs. Never expose admin tokens or private customer/order metadata.

Canonical read operations are `search_products` with `{query, limit}` and `get_product` with `{product_id}`. UCP lookup accepts `{ids}`. UCP catalog responses encode price as integer minor units and variants explicitly. Do not multiply every currency by 100: the current translator supports USD, EUR, GBP, ILS, CAD and AUD; other currency exponents need an explicit implementation.

## Discovery and capabilities

The authenticated Auteric control API returns `{document, publish_url, ...}` from `/api/commerce/stores/{store_id}/discovery` when the configured service uses its default prefix. Confirm the actual API prefix and authentication with the operator. Publish `document` as JSON on the merchant hostname, normally `/.well-known/ucp`. The service supplies the signed endpoint under `/agent-commerce/{domain}`.

The profile contains `ucp.version`, `ucp.services`, `ucp.capabilities`, `ucp.payment_handlers`, plus `auteric_domain_verification` and `auteric_attestation`. Current UCP version is `2026-08-25`; use the version returned by the service. Declare catalog search only when `search_products` works, and catalog lookup only when both `get_product` and `lookup_products` work. Cart requires `create_cart`, `get_cart`, `replace_cart_items`, `cancel_cart`. Full checkout requires create/get/update/complete/cancel checkout; a human checkout URL alone does not qualify.

## Signed exposure versus runtime protection

`auteric_attestation` uses `alg: Ed25519`, `kid`, `payload`, `signature` and informational `public_key`. Its payload is `{kind: "auteric.ucp.exposure.v1", domain, endpoint, store_id, capabilities}`. Signing uses UTF-8 JSON with sorted keys and compact separators. Verification requires an independently trusted Auteric public key, exact hostname, and binding to the declared service endpoint. An embedded public key is not a trust anchor.

Only the Auteric service issues the exposure signature. Never copy development keys or self-sign a merchant certificate. The signature verifies the managed exposure declaration; it does not prove policy enforcement. Runtime protection requires service-side evidence of authenticated routing and allow/deny enforcement for this merchant. Ask Scanner to check those trusted runtime records after connection. This offline kit cannot issue such records.

## Source references for maintainers

Auteric platform: `auteric-commerce-sdk/src/auteric_edge/models.py`, `services/commerce/ucp.py`, `services/commerce/app.py` (`discovery_document`), `services/commerce/attestation.py`. These files belong to the platform, and are not bundled here. The merchant needs its own application and an Auteric connection, not the private monorepo.
