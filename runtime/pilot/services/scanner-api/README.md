# Auteric Agentic Commerce Scanner

The public scanner is the free acquisition surface. It observes public storefront,
catalog, protocol, checkout and security signals without credentials or purchases.
It must report private runtime controls as **not verified**, unless a connected test
has produced evidence.

## Protocol scoring boundaries

The report keeps three different claims separate:

- Catalog readiness scores only the product evidence available on the sampled
  storefront/catalog page. A conforming protocol does not fill missing product
  descriptions, identifiers or availability in that sample.
- UCP conformance scores the public manifest, current version, structural or
  official schema validation, authority binding, capability/checkout/payment
  declarations, transport reachability and root signing keys. The grade is an
  automated Auteric assessment, not an official certification.
- Runtime protection is reported as not detected until connected Auteric tests
  produce transaction evidence. A product name, hostname or UCP declaration is
  never treated as proof that Auteric enforcement is behind the checkout.

ACP detection is deliberately conservative. The public scanner checks explicit
storefront declarations and non-mutating public endpoint signals; ACP does not
currently define a universal public discovery manifest, so absence is reported as
"not detected" rather than "unsupported".

## Shopify protection handoff

`Protect my store` is the handoff to the merchant-authorized Auteric product. Keep
the Shopify OAuth flow and Shopify Function implementation in the Shopify app
service/repository; the scanner only detects the platform and links to its public
install entrypoint.

Set the production install entrypoint with:

```sh
AUTERIC_SHOPIFY_INSTALL_URL=https://app.auteric.com/shopify/install
```

Only an absolute HTTPS URL without embedded credentials is exposed to the browser.
When the variable is missing or invalid, the UI explicitly says that installation
is not connected and links to the Shopify protection explainer instead.

The Shopify app should combine:

- Auteric policy and authorization decisions for agent identity, delegated scope,
  spending limits and transaction integrity.
- A Shopify Cart and Checkout Validation Function as the server-side enforcement
  boundary that blocks checkout when the required Auteric decision is absent,
  invalid, expired or no longer matches the cart.
- Connected verification and audit evidence before the scanner reports a control
  as protected.
