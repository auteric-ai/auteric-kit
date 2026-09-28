# Merchant runtime matrix

Support levels for the merchant runtime SDKs and the installation pipeline
(inventory → bind → validate → verify). The wording is deliberate:

- **fixture-covered** — an automated fixture in `tests/installation/`
  exercises this stack on every change. Read the linked suite for exactly
  what is asserted.
- **tested** — the package has its own automated test suite (unit +
  conformance vectors) that runs on every change.
- **supported** — intended to work and inside the support perimeter, but the
  evidence is one of the above rather than a production track record.

Nothing on this page claims general availability beyond the listed evidence.

## Installation pipeline (installation-time SDK)

| Stack | Inventory | Binding / codegen | Static validation | Contract acceptance (`auteric verify`) | Evidence |
| --- | --- | --- | --- | --- | --- |
| Express (JavaScript) | fixture-covered | fixture-covered | fixture-covered | fixture-covered end-to-end (real HTTP, dev mode) | `tests/installation/binding/`, `tests/installation/acceptance/` |
| Fastify (TypeScript) | fixture-covered | fixture-covered | fixture-covered | pending (node harness exists; no fixture yet) | `tests/installation/fixtures/fastify-ts` |
| Next.js server routes | fixture-covered | fixture-covered | fixture-covered | pending (no fixture yet) | `tests/installation/fixtures/next-routes` |
| FastAPI | fixture-covered | fixture-covered (generated code compiles) | fixture-covered | pending (python harness not in this version) | `tests/installation/binding/` |
| Django | fixture-covered | not fixture-covered | not fixture-covered | pending | `tests/installation/fixtures/django` |
| Go `net/http` | fixture-covered | fixture-covered (`gofmt` clean) | fixture-covered | pending (go harness not in this version) | `tests/installation/binding/` |
| GraphQL-backed commerce | fixture-covered (route merging) | — | — | — | `tests/installation/fixtures/graphql-commerce` |
| Monorepo split frontend/backend | fixture-covered | fixture-covered | — | — | `tests/installation/fixtures/monorepo-split` |
| Static-only storefront | fixture-covered (no backend invented) | not applicable — catalog-only or explicit companion backend per ADR-01 | — | — | `tests/installation/fixtures/static-only` |

## Merchant runtime SDKs (serve MEP/1 inside the merchant backend)

| SDK | Language | Status | Evidence |
| --- | --- | --- | --- |
| `@auteric/merchant-node` | Node (Express, Fastify, Next drivers) | tested | `packages/merchant-node` unit + conformance suite; used end-to-end by `tests/installation/acceptance/` |
| `auteric-merchant` | Python | tested | `packages/merchant-python` (see its README) |
| `merchant-go` | Go | tested | `packages/merchant-go` (see its README) |

## Runtime selection (ADR-01)

The binding planner picks the runtime from the authoritative backend, never
from the storefront:

- Backend that can receive HTTPS → **native runtime in the backend's own
  language** (`native_node`, `native_python`, `native_go`).
- Private network with no ingress → **outbound_worker**, chosen only on that
  deployment constraint.
- Static storefront with no backend → limited read-only catalog, or an
  explicit companion backend. A Vite/static build is never evidence of a
  backend.

The contract acceptance runner currently executes dev-mode HTTP scenarios
against Node composition roots only; Python and Go bindings report `pending`
with an explicit reason rather than a false pass.
