# Model-driven Custom Connect

The frontier coding model currently working in the merchant repository performs
the interpretation. Connect supplies deterministic inventory, pinned contracts,
generic execution, and acceptance. Do not spawn another coding agent.

Run the installed/bundled CLI from the merchant repository to create the inspection dossier. Resolve `scripts/cli/bin/auteric.js` relative to this Skill, or use the public command:

```sh
npx --yes github:auteric-ai/auteric-kit connect --domain STORE
```

The CLI returns `implementation_required` with `auteric/.state/model-dossier.json`
and a `continuation` for the current model. This is an internal model handoff,
not the end of the user's Connect request. Continue immediately in this task;
never ask the user to create a plan or run a second command.
Read actual source behind candidate routes: composition root, services, auth,
session creation, ownership checks, transaction wrapper, audit, stock, prices,
currency, action identity and resource revisions. Trace deployment separately.

Use the pinned registry under `runtime/contracts/registry`, input/output schemas
under `runtime/contracts/schemas`, and conformance vectors under
`runtime/contracts/vectors/conformance`. The dossier names the runtime source.
Read its `src/module-adapter.js`, `src/bridge-helpers.js` and `src/contracts.generated.js`.
The reusable acceptance entry point is `src/connect/local-acceptance.js`.

For every registry operation record `classification`, `reason`, and source
`evidence`: `http_api`, `internal_service`, `unsupported`, or
`implementation_required`. Only the first two may enter the binding. Similar
names do not prove semantics. Untested capabilities stay disabled.

Generate a reviewed JSON plan at ignored `auteric/.state/adapter-plan.json`:

- `files`: map of `auteric/adapter.mjs`, `auteric/connection.json` and
  `auteric/tests/fixtures.mjs` to their exact text.
- `hooks`: reversible `{path,before,after}` patches, each matching exactly once.
- `capabilities`: classification records for every pinned registry operation.

Resume the original Connect command internally. It automatically loads the
plan, installs owned files/hooks and runs local acceptance. `--adapter-plan`
remains an internal compatibility option; do not expose it as a required user step.
Do not manually overwrite files. Ownership is recorded before artifacts/hooks are written. Rerun requires
disconnecting the unchanged owned integration first.

Adapter interface `auteric-adapter/v1` exports:

- `schema`, `writes`, `auth`, `session`, and optional `errorMap`.
- `merchant(services)`: operation callbacks `(ctx,input)` calling the merchant's
  existing services synchronously inside its existing transaction wrapper.
- `projections[operation](raw, helpers)`: canonical response mappings, executed
  in the generic runtime. Helpers provide `money(minorUnits,currency)` and
  `enum(authoritativeValue,mapping)`.
- Optional `pagination[operation] — called with raw` returns `{items,total}`. The runtime
  supplies bounded pagination, query-bound cursors and canonical price filters.

`connection.json` uses `auteric-module-connection/v1`, pins `registry_digest`,
names `adapter`, `operations`, `writes`, `fixtures`, and the local application
factory in `application`: `module`, `factory`, `databaseOption`, `adminTokenOption`.
The local MVP factory must support an isolated database and test admin credential.

For internal services expose only `{app,registerRoute,services}` at the existing
composition root. The small Auteric-owned private transport template is copied as
`auteric/private-hook.mjs`. It mounts authenticated routes through the merchant's
own wrapper. It contains no SDK, domain logic, sessions, or transaction engine.
The generated adapter decides the merchant call; the original wrapper remains
authoritative for auth, ownership, transaction, idempotency and audit.
Before choosing the mounting location, locate every terminal API 404 handler and
SPA/static fallback. Mount the private routes after the application services and
transaction wrapper are initialized, but before those terminal handlers; a hook
placed only before static assets can still be intercepted by an earlier API 404.
Never accept owner/session identity from the canonical request or browser cookie.

Fixtures select a real safe product and provide authenticated inventory and cart
oracle setup. Canonical assertions and reports remain in Auteric. Run:

```sh
node /path/to/auteric/kits/auteric-kit/bin/auteric.js connect --local-acceptance \
  --runtime-source /path/to/compatible/auteric \
  --runtime-commit 81fbacaff3a81670e49cf34afe566ac04ece265f
```

Normal acceptance reuses the qualified immutable generic runtime image. The installed Kit includes the versioned Gateway test harness, pinned Python dependencies and canonical vectors. It prepares its private cache outside the merchant repository automatically. No Auteric checkout or private-repository access is required. Node 22.13+, Python 3.11+ and a running Docker daemon are local prerequisites. A source image build is only the unqualified development fallback; merchant files are
mounted, never copied into that image. Gateway/MCP use real local sockets and the
existing signed bootstrap journey. Reports are under ignored `auteric/.state/`.
No AWS, public domain, release qualification or hosted redeployment is involved.
Gateway test dependencies are installed automatically into the versioned Auteric cache. Explicit source/baseline flags are development overrides only.

`disconnect` removes unchanged owned artifacts/state and reverses exact hooks,
preserving unrelated edits. An edited owned artifact/hook refuses cleanup.

## Prepare the normal merchant deployment in the same request

Read [deployment preparation](deployment-preparation.md). Connect now always
emits an owned deployment plan after acceptance, including a continuation when
references are missing. Resolve the model-owned blockers in this same request.
Use the reusable infrastructure renderer rather than generating merchant-specific
provisioning scripts or requesting manual copies of references from the owner.

After local acceptance, continue preparing the existing deployment. Do not report
Connect complete after acceptance alone. Read the actual Dockerfile, startup,
CI deployment, task definition/Compose services, reverse proxy, persistence and
secret delivery. Never fabricate image digests, ARNs, domain ownership or account
credentials. Use the reusable module binding/enrollment and managed runtime;
do not leave production pointing at `--local-acceptance`.

Generate `auteric/deployment.json` in the owned plan using the runtime deployment
schema and real installation references. Connect finds this file automatically,
runs local acceptance, release/Control compatibility gates, owner enrollment,
and renders `auteric/runtime-connection.json` plus `auteric/compose.yaml` or
`auteric/task-definition.json`. The module `connection.json` remains the adapter
configuration; its bytes and adapter bytes are fingerprinted together.
Enrollment transmits only that fingerprint and canonical operation/effect subset.
No executable merchant code goes to Control. Keep unsupported capabilities disabled.

The generic manager loads the mounted adapter, checks its fingerprint against
registration before starting children, supervises Bridge/Sidecar and refreshes
credentials/config/evidence through the existing enrollment path. It serves exact
Control-issued discovery at `/.well-known/ucp`. Use one generic immutable image;
changing the adapter requires only the merchant deployment, not a runtime rebuild.

For Compose, attach the overlay to the merchant's existing deploy command,
set `AUTERIC_APPLICATION_TOKEN` for the merchant from the same private secret
file mounted as `AUTERIC_APPLICATION_TOKEN_FILE` in runtime, and configure
`AUTERIC_RUNTIME_ORIGIN` to that private runtime service. Preserve the existing
merchant network and persistent business database. Auteric execution state is Gateway-owned (`gateway/v1`); the runtime uses
temporary configuration/cache only. Installation credentials use a dedicated
private identity directory. Never publish the Bridge port.

For ECS, the model prepares an owned Dockerfile patch that copies `auteric/`
into the final merchant image, excludes `.state`/`discovery` through `.dockerignore`,
and exports only adapter/config to `VOLUME ["/auteric-integration"]`, with the
merchant user's directory/file ownership. The renderer mounts this task-local
artifact volume read-only into `/integration` in the generic runtime. This volume
contains immutable merchant code, not durable business/runtime data. Add the real
shared private application and installation secret references, `gateway/v1`,
`adapter_mount`, and the existing `task_definition` source to deployment config.
The renderer preserves the original merchant command, supplies private runtime
origin/auth, and adds one runtime container. Make the existing workflow deploy
`auteric/task-definition.json`, updating the selected merchant image by name.
Do not replace a healthy deployed persistent merchant task with this clean
fixture's historical ephemeral database settings.

Public routing must send `/api/auteric/v1/*` and `/api/auteric/agent/v1/*` to the
managed runtime port. The owned private hook optionally proxies `/.well-known/ucp`
from `AUTERIC_RUNTIME_ORIGIN`, before the merchant SPA/static fallback. It returns
503 if runtime discovery is unavailable and preserves exact service JSON.
An existing UCP route must be deliberately integrated, not silently shadowed.
A frontend-only site needs its actual hosting/proxy route prepared instead.

A request to Connect authorizes preparing these changes and local tests, not
silently redeploying an unrelated live merchant. Report concrete missing image,
service/credential/reference or deployment prerequisites. Do not label a generated
task, a local managed-runtime test, or signed local profile a public connection.
The installed/public package must include this Skill and compatible core bytes.

The single user request is `auteric connect --domain STORE` inside the current
model's merchant task. The model performs internal CLI calls as needed and
repairs only the integration. User-facing `--adapter-plan` and manual reruns are
not part of normal onboarding. Cloud publication/activation needs real verified
references and runtime proof.
