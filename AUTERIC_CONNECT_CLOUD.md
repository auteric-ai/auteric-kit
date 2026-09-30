# Connect against hosted Control — acceptance and prerequisites

Release candidate `0.6.5-rc.3`; hosted minimum acceptance passed on 2026-09-30; source `56a90d5`, release workflow `36748546024`. Run from the merchant repository, using Node 22.13+ and Python 3.12. The minimum currently supports reviewed Node ESM services and an Express `server/app.js` entry. Other architecture must stop visibly for integration review rather than receive a protection claim.

The Control service is `https://control.auteric.com`. No local Control or local Gateway is required. Local addresses refer only to the merchant's Sidecar and private Bridge.

Merchant prerequisites:
- Existing catalog/cart business functions pass binder review; choose a disposable safe product (no payment/order test).
- Public merchant HTTPS origin serves the generated `/.well-known/ucp` JSON; restart the merchant after the recorded route is added.
- An HTTPS origin routes to the merchant Sidecar on local port 8089. Forward only the Sidecar, never private Bridge port 3101. The signed Native and exact verification ingress perform their existing authentication; do not add an unauthenticated commerce proxy.
- Keep the supervised integration process and its durable `.auteric/state` volume running. This minimum is one merchant host, not rolling multi-task production certification.
- The account owner signs into hosted Control and approves the CLI pairing code.

Released candidate command (run inside the merchant repository):

```sh
npm exec --yes --package=https://github.com/auteric-ai/auteric-kit/releases/download/connect-minimum-0.6.5-rc.3/auteric-cli-0.6.5-rc.3.tgz -- auteric connect \
  --sidecar --serve --no-agent --approve-adapters \
  --environment staging --domain YOUR_STORE_DOMAIN \
  --sidecar-public-url https://YOUR_SIDECAR_HOST \
  --product-id SAFE_PRODUCT_ID --query SAFE_SEARCH_QUERY
```

`--api-url` defaults to hosted Control. Scanner defaults to `https://scanner.auteric.com`. Discovery key is obtained from the trusted Control HTTPS origin, separately from merchant UCP; `--discovery-key FILE` overrides with an independently pinned key.

Success requires the existing five-stage Connection Test plus allow/block, replay/session/auth negative probes; independent public UCP → advertised hosted MCP → Gateway → Sidecar → private Bridge → actual catalog result; a real Scanner scan and authenticated runtime receipt. Status must reach `minimum_verified`. A running process, successful login, published file or health 200 alone is insufficient.

Disconnect uses the same released CLI `auteric disconnect`. It disables access, revokes installation credentials, invalidates Scanner protection, restores recorded source, removes unchanged owned files and preserves durable audit/business outcomes. Merchant edits must be preserved and reported.

Artifact SHA256: `6ae169e58dd91e84c43d2184a83bfd1860dca4b2ebefd9ca2390e351c79a848c`. Choose a query that actually matches the merchant catalog; the independent verifier requires the selected product in the real search response. Installed plugin `0.7.3` was not updated; use this explicit package URL. The bundled local PILOT guide is not the hosted command.
