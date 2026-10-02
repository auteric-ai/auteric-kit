# 0.7.6 Deployment preparation continuation

Connect now always creates an owned deployment plan after local acceptance.
Missing references produce a continuation for the current coding model, rather
than a successful-looking terminal result. The Skill traces the deployment,
compares authorized live metadata, reuses existing resources and prepares
reversible workflow/proxy changes before ending the request.

The reusable ECS infrastructure template can prepare enrollment/application
secrets, narrow role grants, and optionally private durable runtime PostgreSQL.
Applying it requires actual infrastructure/cost authorization. It does not
modify merchant business data, deploy the merchant service or invent enrollment.
ECS preparation now includes explicit ingress intent. Owned plans can refresh;
merchant edits remain protected and disconnect can remove unchanged artifacts.

The immutable Sidecar/Bridge image and deployed Control/MCP are unchanged.
No merchant Connect, merchant deployment or infrastructure provisioning was
performed for this release. New infrastructure templates are locally tested;
applying them to a new merchant still needs provider validation and live proof.

# 0.7.5 Portable Connect

The Kit includes the immutable generic Gateway harness and canonical contracts.
Local acceptance no longer searches for or requires an Auteric monorepo.
First use prepares a versioned cache outside the merchant and pulls the existing
public runtime image automatically. Explicit source flags are development-only.

The Gateway input schema now preserves `expected_revision` for `add_to_cart`.
The enforcement implementation is unchanged. The independent harness passed all
38 Custom Store scenarios, including stale-revision rejection and five MCP tools.
Cloud Control/MCP deployment and public clean-install qualification are recorded
separately; no merchant deployment is performed by this release.

# 0.7.4 Connect qualification

Custom Connect defaults to the current coding model plus a small module adapter.
The CLI supplies deterministic installation, generic runtime and contract tests.
The published qualified runtime is reused; no merchant-specific image is built.
The local Gateway test still requires an existing compatible Auteric monorepo.

The packaged CLI was installed in a clean consumer and run with the same domain
and `--no-agent` arguments on Custom Store. All 38 scenarios passed, including
five MCP operations and two runtime restarts. The merchant diff was unchanged.
This verifies local code preparation, not public merchant activation.

The complete generated installation request was tested against the exact deployed
Control image: owner browser PKCE, store creation, policy review, registration,
native/Sidecar configuration, module enrollment and signed discovery passed.
Unknown fields and unauthorized requests remain rejected. `runtime_kind` is no
longer sent; the full registration schema is pinned and validated locally.
64 JavaScript and 32 Python tests passed in the standalone public repository.

No merchant deployment is performed by Connect. Owner enrollment requires a real
owned target and valid deployment configuration; an existing live installation
is preserved rather than silently overwritten. Public discovery and protection
require deployment and runtime verification.

---

# 0.6.1 GitHub release

This update copies a signed UCP document into an existing built static storefront
as well as its source `public` directory, then verifies the served route. It
creates a private read-only Gateway MCP grant after connection verification, while
keeping Integration MCP optional. A local Vite/Express test returned the exact
signed JSON from port 9020 and listed/called `search_products` and `get_product`
through the dedicated Gateway MCP on port 8102. Other detected cart/checkout
routes remained inventory because their adapter contracts were not validated.
This is local evidence only; public domain ownership and production deployment
remain unverified.

The GitHub package bundles the CLI and SDK. No npm publication or separate plugin
installation is necessary for the terminal Connect command.

Validated locally: automatic Codex adapter creation for an unfamiliar custom REST
store; actual catalog API from the existing Vite/Express store on port 5173;
isolated owner sign-in with normal code approval; separate MCP service execution;
local signed UCP route; repeat connection without re-pairing, duplicate Store or
mapping activation; worker restart and credential revocation. The acceptance
harness is in `acceptance/test_onboarding.py`, with the custom merchant source in
`acceptance/fixtures/custom`. Pairing codes stay in test-process memory.

The standard suites cover 48 deterministic catalog combinations, schema rejection,
private session state, mapping pinning, exclusive connection locks and bounded
assistant execution. The platform acceptance suite additionally exercises all 20
canonical operations with a synthetic nonfinancial connector. That fixture is not
proof that arbitrary real carts or payment systems work.

Live model execution was tested with Codex only. Claude, Cursor and Copilot command
adapters still need validation with installed, authenticated provider CLIs.
The connector is foreground and needs process supervision for persistent hosting.
Production ownership, HTTPS publication, hosted deployment, real merchant write
journeys and payments were not exercised. Payment capture/orders/refunds remain
unsupported. Release of the kit is not a production deployment of the platform.

A platform fix allows loopback product links only for a development service and
sandbox Store. It must be included in the running local platform separately from
the kit. Public-mode URL rules stay unchanged.
