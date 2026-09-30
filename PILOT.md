# Connect single-command release candidate

CLI candidate: **0.6.5-rc.7**. Plugin source candidate: **0.7.4-rc.1**.
A published candidate and local tests are separate from passing public acceptance.

## Supported boundary

Node 22.13+ (Node 22 is tested), Python 3.12+, an unambiguous Node ESM Express
application with its own start script, source-traced original commerce routes,
and authoritative product/cart output that can satisfy the locked MEP/1 contract.
The mapper discovers files, factories and route wrappers from source; no particular
merchant file, function, product or domain is required. Ambiguous authentication,
missing money/stock/identities or unsupported layouts fail closed.

The Bridge calls the original HTTP routes. Their transaction, audit, buyer session
and idempotency boundaries remain in the merchant application. Durable canonical
ID aliases map back to actual merchant IDs before execution. Buyer cookies remain
private and isolated by installation and pairwise buyer; neither cookies nor ID
mapping databases belong in Git.

## Run

Use the candidate's downloaded, digest-verified package from the merchant root:

```sh
node /path/to/verified/package/bin/auteric.js connect --domain YOUR_HTTPS_MERCHANT_HOST --no-agent
```

The domain must route to this checkout's original storefront. A temporary
storefront tunnel may be provisioned by an acceptance harness. Connect starts
the merchant's own command and a temporary Sidecar tunnel automatically, selects
a real available product, and keeps the Bridge on **127.0.0.1:3101**. Install
cloudflared locally or select its binary with AUTERIC_CLOUDFLARED. Never tunnel
the Bridge. Use PORT to match a prepared storefront tunnel's origin port.

Run against https://control.auteric.com. Keep browser authorization enabled.
A valid account session may be reused with the owner's authorization; new
acceptance attempts still need fresh installation/runtime state. Account-session
reuse does not establish current runtime protection.

## Acceptance gates

Only supported operations are installed. This boundary currently supports search,
product lookup, cart creation/read and add-item. Partial catalog acceptance also
requires Control's explicit operation_subsets support; it never widens capabilities
for an older server. Unknown operations and checkout/payment remain disabled.
Nonempty initial cart items and unsupported revision preconditions are rejected;
add-item requires an authoritative variant belonging to the requested product.

The CLI requires all of these before status=minimum_verified:

- Original merchant tests/build and live product selection.
- Gateway connection journey with exact merchant receipts and operation-bound
  policy denial, replay, authorization and applicable buyer-ownership probes.
- Current signed public JSON UCP and an independent call through its advertised
  cloud MCP endpoint to the actual merchant catalog.
- Current Scanner evidence accepted and connection health protection=active.

Private evidence lives under .auteric. Foreground Connect keeps the added runtime
serving; Ctrl-C stops its child processes and temporary Sidecar tunnel. Evidence
of a completed run does not mean the temporary service remains online later.

## Clean attempts and review

Use a new merchant worktree/branch from freshly fetched origin/main and a private,
isolated installation directory for every attempt. Run Connect once. Preserve a
failed attempt, credentials, ledgers and unresolved actions; repair maintained Kit
source, publish an immutable new candidate if code changed, then start a new
attempt. Do not prepatch merchant code or copy installation state between attempts.

Commit only generated public integration source, discovery and safe ownership
configuration from the successful attempt. Exclude .auteric, credentials, cookie
and merchant databases, node_modules and logs. No permanent production deployment
is part of this temporary acceptance workflow.
