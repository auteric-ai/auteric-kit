# 0.6.0 GitHub release

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
assistant execution. The platform acceptance suite additionally exercises all 11
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
