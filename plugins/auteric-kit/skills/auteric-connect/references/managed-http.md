# Managed HTTP Connect

The development CLI defaults to a bounded declarative HTTP mapping for custom
stores. It never invokes a coding agent or copies a runtime, vendor tree or
lifecycle script into a merchant repository. Existing native integrations keep
using their native path. Explicit `--legacy-connector` retains the previous
outbound connector installer for an installation that intentionally uses it.

For this development branch, use the maintained CLI checkout or the actual
installed plugin path after running `npm ci --ignore-scripts` in its
`scripts/cli` directory. Node 22.13+ is required. The development bytes have not
been published; GitHub/npm or the installed cache can still contain an older
workflow. Do not describe those as updated by a source edit.

Start from the authoritative merchant backend. Inventory stays local, including
admin/auth/payment routes; only reviewed MVP mappings and digests are registered
with Control. OpenAPI JSON can provide an `x-auteric` common session/output
profile and per-operation input/output/auth mapping. Without enough evidence,
Connect returns `implementation_required` or `selection_required`. Do not
implement a client adapter or guess fields to turn this into a successful run.

The reviewed MVP comprises `search_products`, `get_product`, `create_cart`,
`get_cart`, `add_to_cart`. Checkout/payment remain outside this profile. Missing
money/currency/availability, required ownership/idempotency/revision scenarios,
unknown schema, unqualified image and incompatible Control stop preparation.

For an explicitly supported mapping, use:

```sh
auteric connect --domain STORE --mapping auteric/openapi.json --deployment auteric/deployment.json \
  --environment staging --test-origin http://127.0.0.1:PORT \
  --test-query QUERY --test-currency USD
```

`--test-origin` must be an isolated merchant sandbox. The shared Bridge tests
create their own buyers and carts and prove translation only. They do not prove
signatures, policy, replay or Gateway enforcement. Never use an existing
customer's cart or a payment/checkout route as verification data.

Release qualification is mandatory before enrollment. Check that the released
runtime and Control both support `auteric-runtime-state/v1`. An unqualified local
image cannot activate public capabilities or substitute for released artifacts.

A qualified Compose deployment uses a fixed runtime digest, existing private
network and Gateway-backed state. Every new repository file belongs under
`auteric/`: connection/deployment config, private `.state/` reports and
enrollment, ignored `discovery/`, and `.gitignore`. Existing merchant files are
read without modification. If input mapping/deployment profiles need creating,
put those under `auteric/` too. Set `secret_ref` to
`file:/ABSOLUTE/MERCHANT/ROOT/auteric/.state/identity/enrollment.json` and
`discovery_mount` to `/ABSOLUTE/MERCHANT/ROOT/auteric/discovery`. The non-root
runtime UID must own the dedicated private identity directory for atomic
credential rotation. No runtime database or persistent execution-state volume
is required. Discovery is served by the runtime from refreshed Control bytes.
The owner credential is used for onboarding
only. The runtime exchanges the limited enrollment for installation-scoped
service identity, fetches exact service-issued configuration, and renews through
explicit APIs. Uncertain renewal blocks subsequent mutation until authoritative
reconciliation. GET config never runs verification or changes domain ownership.

`auteric disconnect` revokes only the installation/enrollment, stops its
owned Compose service, removes matching generated connection/deployment,
bootstrap and discovery bytes, and preserves edited files and durable audit/state.
It never removes an external volume or merchant data. If stop fails or Compose
was edited, report `cleanup_pending` and the exact remaining action; rerunning
disconnect resumes without repeating a completed revocation. Prepared-only
local state can disconnect without owner pairing.

Preparation ends with `deployment_pending`, then the merchant runs their normal
deployment workflow. Runtime publishes exact Control-issued UCP bytes through a
discovery mount/route. `verified` and `production_ready` require separate current
service evidence. Do not claim a 5–15 minute installation until published-image,
clean-install, public UCP/MCP/Gateway and persistence qualification passes.
