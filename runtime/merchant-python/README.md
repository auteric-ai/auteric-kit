# auteric-merchant

Merchant-side SDK for the Auteric Merchant Execution Protocol (MEP/1, locked
in `docs/commerce/protocol/merchant-execution-protocol-v1.md`). The SDK owns
the plumbing — Ed25519 execution-JWT verification, request-hash checks,
contract validation, idempotency ledger, discovery serving — and the merchant
writes small adapters, one per operation:

```python
async def add_to_cart(ctx, input, path_params):
    # ctx.principal is the verified merchant-local principal (never from the body)
    cart = await add_line(ctx.principal, path_params["cart_id"],
                          input["product_id"], input["quantity"])
    return cart_to_output(cart)  # validated against the output schema on the way out
```

## Import safety

**Importing `auteric_merchant` never starts workers, threads, polling loops,
sockets, browser/onboarding flows, or network calls.** The framework drivers
import FastAPI/Django lazily inside their factory functions; the top-level
package needs only `cryptography`. A test (`tests/test_import_hygiene.py`)
asserts this on every run.

## Quickstart (FastAPI)

```python
from fastapi import FastAPI
from auteric_merchant import (
    Installation, OperationBinding, TrustBundle, MerchantRuntime,
    SQLiteExecutionStore, MapPrincipalResolver,
)
from auteric_merchant.fastapi_driver import create_auteric_router
from auteric_merchant.well_known import create_well_known_router

installation = Installation(
    installation_id="install_...",
    store_id="store_...",
    environment="production",
    manifest={
        "add_to_cart": OperationBinding(
            method="POST",
            path="/api/auteric/v1/carts/{cart_id}/items",
            contract_version="1.0.0",
            binding_digest="sha256:...",  # as registered with Auteric
        ),
    },
)
trust = TrustBundle(
    issuer_allowlist=("https://gateway.example.invalid",),
    keys={"gw-key-1": bytes.fromhex("...")},  # pinned Ed25519 public key, from config
)

runtime = MerchantRuntime(
    installation=installation,
    trust=trust,
    adapters={"add_to_cart": add_to_cart},
    execution_store=SQLiteExecutionStore("/var/lib/mystore/auteric-executions.db"),
    principal_resolver=MapPrincipalResolver({"buyer_pairwise_...": "customer-123"}),
)

app = FastAPI()
app.include_router(create_well_known_router(discovery_service))  # before any catch-all
app.include_router(create_auteric_router(runtime))               # /api/auteric/v1/...
```

Django: `auteric_merchant.django_driver.make_auteric_view(runtime)` (async) /
`make_auteric_view_sync(runtime)` (WSGI), plus `DjangoExecutionStore`, which
runs on the merchant's own DB connection and participates in the merchant's
transactions (adapters may wrap work in `transaction.atomic()`; the ledger
commits/rolls back with the cart update). See the module docstring.

## Execution store

- `InMemoryExecutionStore` — tests/single-process development.
- `SQLiteExecutionStore(path)` — reference implementation for real
  single-node deployments (stdlib sqlite3).
- `DjangoExecutionStore` — merchant DB via `django.db` (multi-node safe; the
  unique key lives in the merchant's primary database).
- Multi-node non-Django deployments must implement `ExecutionStore` against
  a shared database with the same atomic-reserve semantics (unique
  `(installation_id, action_id)`).

## Sidecar deployment

Custom stores can keep Auteric outside the application process. Install the
`sidecar` extra and start the packaged server with an immutable configuration:

```sh
AUTERIC_SIDECAR_CONFIG=/etc/auteric/sidecar.json auteric-sidecar --port 8080
```

The configuration contains pinned public keys, installation identity, verified
execution profiles, and **secret references only**. References use `env:NAME`
or `file:/run/secrets/name`; literal credentials are not accepted as references.
The process exposes `/health/live`, `/health/ready`, and the MEP/1 routes below
`/api/auteric/v1`. The container image runs as a non-root user.

For a Node merchant service, `createServiceBridge` exposes selected business
functions on an authenticated loopback listener. It is intentionally not a
second policy engine: the sidecar remains responsible for signed identity,
contract validation, idempotency and execution controls.

## Security notes

- Only `alg: "EdDSA"` is accepted; `jku`/`x5u` are always rejected; the
  `kid` must resolve to a key in the pinned trust bundle. No JWKS URL is
  ever fetched.
- Clock skew is ±5s and the token window `exp - iat` must be ≤ 30s. `jti`
  replay is rejected via a nonce cache (TTL ≥ 60s).
- Identity comes only from the verified token (`sub`); the raw request body
  is never trusted for identity.
- The request hash (MEP/1 §3) covers method, canonical path, canonical
  query, and the raw body bytes. The trusted proxy prefix (`root_path`)
  comes from installation config, never from request headers — the SDK does
  not honor `X-Forwarded-*`.
- Error responses follow the MEP/1 §5 envelope and never include stack
  traces, credentials, or payment-provider details.
- The discovery document (`/.well-known/ucp`) is served only if it carries a
  valid control-plane Ed25519 signature over the JCS-canonical document, is
  within `expires_at`, and matches the local `store_id`/`hostname`. The
  merchant never signs the document locally. Cache TTL ≤ 30s;
  last-known-valid is served only while still within expiry.
- Response bodies from adapters are validated against the output schema
  before hitting the wire; invalid adapter output becomes `UPSTREAM_ERROR`.

## Contracts vendoring

The schema validators are generated by `packages/commerce-contracts` and
vendored into this package at build time:

    python tools/sync_contracts.py          # refresh src/auteric_merchant/contracts_generated.py
    python tools/sync_contracts.py --check  # drift check (runs in tests)

The published wheel bundles `contracts_generated.py`, so it has no runtime
dependency on the contracts repo layout.

## Development

    .venv/bin/python -m pytest packages/merchant-python -q
