# Connect a PSP with a shared commerce backend

Use `auteric connect psp --domain PSP_BACKEND_DOMAIN --environment production`
inside the PSP backend's coding task. Bare `auteric connect psp` also starts
inspection. The current model owns the continuation; the CLI does not spawn a
second model. This mode connects PSP membership to the existing Store boundary.

## Source of merchant membership

Inspect the PSP's authenticated merchant/account inventory and tenant resolver.
Record the actual account ID and commerce Store for each selected merchant.
Do not infer membership from a domain, email suffix or shopper-supplied account ID.
Use an existing Control Platform entity or create one under the real owner's
organization. Stores must belong to that Platform subtree and the same environment.
Independent merchant organizations require the existing reviewed relationship
workflow; this mode cannot move Stores or grant cross-organization access.

The model prepares ignored `auteric/.state/psp-plan.json`, for example:

```json
{
  "schema": "auteric-psp-plan/v1",
  "name": "Merchant PSP",
  "backend_origin": "https://api.psp.example",
  "environment": "production",
  "platform_resource": "existing-control-platform-id",
  "stores": [
    {"store_id": "existing-control-store-id", "merchant_id": "real-psp-account-id"}
  ]
}
```

For a new Platform, omit `platform_resource`. New Store entries use `name`,
`domain` and `merchant_id` instead of `store_id`. These values must come from the
selected merchant inventory and owner authorization. Existing Stores require
their actual Platform; never create a new parent or silently move them.
No credentials or payment data belong in the plan.

Resume the same command internally. It checks `auteric-psp/v1`, uses the existing
Control owner sign-in, configures the Platform and registers each approved Store
mapping. `authentication_pending` requires the real owner's Control approval;
resume after approval without changing the plan. Partial registration retains
created identities, and unchanged writes are idempotent. The server requires
both Store connection authority and Platform approval. Duplicate PSP accounts,
environment mismatches, reassignment and changed provider origins fail closed.

`auteric/psp.json` contains Control-returned Store/account/backend mappings and
binding digests. It is integration configuration, not an access grant. The CLI
does not create owner roles or send invitations. Prepare Store-scoped invitations
through existing Control management when requested; provider onboarding must not
give every shop owner Organization Owner access to the PSP.

## Shared adapter, separate execution authority

Implement one reusable adapter for the PSP API. Inspect the real catalog,
inventory, cart, session, transaction and order boundaries before mapping an
operation. Bind the PSP merchant account at the authenticated server boundary;
never accept it from canonical shopping input, cookies without validation, or a
frontend Store selector. Reuse the PSP's existing tenant/session checks.

The installation CLI exports `resolvePSPAccount(manifest, verifiedContext)` with
`verifiedContext.storeId`; this helper only resolves the pinned account after the
existing runtime/merchant boundary has verified context. It authenticates nobody.
For an existing MEP adapter, derive the Store from its verified installation
configuration, and the merchant tenant from the original server's authenticated
session. Do not add unsupported context fields to MEP or bypass buyer ownership.

Test at least two isolated merchant accounts on the shared backend: the same
product/cart identifier in each account must not cross tenant boundaries. Include
wrong Store, denied account, duplicate/replayed writes and unavailable backend.
Run the normal local acceptance for each selected capability. Reuse prepared
runtime/Bridge code and the existing merchant deployment.

Keep one Store-scoped runtime/connector and signed discovery/credentials per
installation. Sharing adapter code does not make a single Sidecar multi-tenant.
Prepare per-Store deployment manifests in separate installation workspaces and
mount the shared adapter; reconcile existing integrations before replacement.
The PSP backend can be common to all these installations.

Membership registration returns `production_ready:false` and a model continuation.
Continue adapter tests and deployment preparation in the same task. Check the
actual Control runtime compatibility before choosing managed runtime enrollment;
missing compatibility is a concrete prerequisite, never a successful connection.
Public verification and activation use the normal Store evidence and permissions.
Catalog and payment APIs alone do not establish cart, fulfillment or order support.
`complete_checkout` is excluded from the current Sidecar release; PSP membership
must not enable payment completion. Report membership, local acceptance, deployment
and verified runtime operation separately.
