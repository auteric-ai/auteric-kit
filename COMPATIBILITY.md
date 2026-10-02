# Supported coding agents

Normal Connect uses the capable coding model already working in the merchant
repository. The shared Skill supplies contracts, adapter interfaces, tests and
deployment helpers. It does not start a second coding agent or implement merchant
business logic in the CLI. A terminal alone returns an implementation continuation.

| Host | Start |
| --- | --- |
| Codex | Install/update Auteric Kit, then request `auteric connect --domain STORE` in the merchant task. |
| Claude Code | Invoke the Auteric storefront command or request the same connection with the shared Skill installed. |
| Cursor / other Skills-compatible hosts | Install the shared Skill and request connection inside the merchant repository. |

The model scans the code, generates small isolated files under `auteric/`, runs
canonical contract tests through the generic image and bundled local Gateway/MCP,
then prepares the existing deployment. Node 22.13+, Python 3.11+ and Docker are
local prerequisites; private monorepo access and AWS are not required locally.

The owner signs in to Control after local acceptance. Authorization is resumable
and verified with PKCE. A login timeout preserves unchanged passing integration
work. Only a real approved account can enroll an installation.

The released runtime uses `auteric-runtime-state/v1` and `gateway/v1`. It needs no
merchant-side runtime database or persistent execution-state volume. Installation
and application credentials still use platform secret storage; Compose may use
its dedicated private identity directory. Merchant business persistence is separate.
Public connection needs the prepared merchant deployment and public verification.
