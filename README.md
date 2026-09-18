# Auteric Kit 0.4.6

`connect` starts at the repository root, detects the storefront and the authoritative
backend, then installs the bundled Python SDK and project skill, scans every canonical
operation, installs a static catalog adapter when supported, pairs with the account
owner in the browser, and runs sandbox mapping and Gateway/MCP checks. It writes
capability and validation reports in `.auteric/`. Keep `auteric connector` running
for continued access. Runtime credentials stay outside the merchant repository.

The plugin carries its CLI at `skills/auteric-connect/scripts/cli/bin/auteric.js`.
The skill invokes that actual installed path; npm publication is not required.
Node 20+ and Python 3.11+ with venv are required; first runtime installation fetches
httpx and pydantic dependencies into a private virtual environment.

Factory and REST integrations use an explicit `.auteric/connector.json` with
supported operations and sandbox `test_inputs`. Route-name matches are candidates;
the coding skill must trace and implement the adapter before they become tools.
Payment capture, orders and refunds are outside this runtime's supported operations.
Production stores are prepared only; activation and deployment require publication
approval and independent production checks.

<p align="center">
  <img src="https://scanner.auteric.com/static/brand/logo_evergreen_auteric-symbol_20260906_transparent.png" width="76" alt="Auteric mark" />
</p>
<h1 align="center">A U T E R I C</h1>
<p align="center"><strong>Auteric Kit</strong> · Agentic commerce connection for custom storefronts</p>

> Brand usage: use the Auteric mark beside the spaced uppercase wordmark, `A U T E R I C`. The shared visual specification is in [BRAND.md](BRAND.md).

## CLI preview for custom storefronts

The repository now includes a Node.js CLI source package. It has **not** been
published to npm. Until the control-plane authentication routes are running,
use the local checkout to inspect a store without changing it:

```sh
node /path/to/auteric-kit/bin/auteric.js connect --domain store.example.com --dry-run
```

For the shortest GitHub-based local flow, run this once from the store repository
root. It downloads the exact CLI from the public kit repository for this run; it
does not require a clone, a Codex cache path, or a global installation:

```sh
npx --yes github:auteric-ai/auteric-kit --localhost --store-url http://127.0.0.1:5173 --serve
```

The flags-without-a-subcommand form means `connect`. It installs project instructions
for Codex and GitHub Copilot (`.agents/skills`), Claude Code (`.claude/skills`),
and Cursor (`.cursor/rules`) without depending on which editor is running.
`--serve` keeps the connector in the foreground after a verified local setup;
keep this terminal open, and use Ctrl-C to stop it. It scans first. If it can
prove and test a supported connector, it opens browser sign-in and continues;
if the store has no traceable commerce API, it stops before creating a Store or
issuing credentials. A plugin installation cannot run this automatically:
Codex deliberately does not grant plugins install-time code execution or browser
authorization.

Run the command in the integrated terminal of VS Code, Cursor, Claude Code, or
Codex, from the repository root containing the frontend and backend. Do not paste
it into an AI chat prompt expecting shell execution. The local storefront and
Auteric Commerce service must already be running. For multiple API candidates,
choose the authoritative backend with `--backend`; separate repositories require
an explicit integration rather than an inferred cross-repository connector.
The bundled CLI does not need a separate Codex marketplace installation.

Local verification first fetches and compares the exact signed document from
the storefront, then retries transient control-plane fetch failures. If the route
still cannot be fetched, setup retains its tested connector and signed UCP,
reports `local_discovery_pending`, and exits nonzero. It never claims the public
domain or ongoing runtime protection from a localhost test.

For a local Express/Vite store with a public `GET /api/products` collection and
`GET /api/products/:id` item endpoint, Connect verifies the live response and
creates a read-only catalog connector automatically. Cart, checkout and payment
are never inferred from route names and remain disabled until their contracts
are explicitly supported and tested.

To test browser authorization against a locally running Auteric Commerce API:

```sh
node /path/to/auteric-kit/bin/auteric.js connect --domain store.example.com --localhost \
  --store-url http://127.0.0.1:5500
```

For a store without any public domain yet, omit `--domain` but keep `--localhost`
and `--store-url`. The CLI registers a stable `local-...auteric.test` test
identifier for this project. It is not a publishable domain, ownership proof,
or production connection. When a real domain is available, connect it as a
separate production Store and publish a fresh service-issued profile.

Use `--api-url http://127.0.0.1:PORT` together with `--localhost` if your local
Commerce API listens on another loopback port. The flag rejects non-loopback
addresses. The CLI opens the API's browser approval page, then creates or resumes
a pending store automatically after you approve the session. The browser opens
that store's dashboard when setup finishes. Neither a password nor the
short-lived session token is stored in the storefront repository. It writes
`.auteric/config.json` with non-secret local IDs and state.

When the authenticated Commerce API returns a signed UCP document, the CLI prepares
`/.well-known/ucp` automatically in the correct static/public directory.
It refuses to replace an unrelated or manually modified profile; a previously generated profile can be refreshed only when its saved content digest still matches. For a plain static site
served from the project root, that file is `.well-known/ucp`; for Next.js it is
`public/.well-known/ucp`. The result must be reviewed and published by the
merchant. In local development only, the API can issue a profile pointing to an
HTTP loopback Gateway. `--store-url` makes the API fetch the local storefront's
`/.well-known/ucp` and compare the parsed JSON with the issued profile.
Run a static server from the store project root, such as
`python3 -m http.server 5500 --bind 127.0.0.1`, before using this flag. A
development signature is cryptographically valid for its test key, but that
key is not a production trust anchor. Local verification never marks the
public domain as owned or enables production routing. Outside local
development, HTTPS and independent public-domain verification are required.

The CLI prepares and tests supported local connectors, but it does not publish a
merchant site, issue production credentials, or enable production traffic. The local
Commerce API must be running separately. Do not advertise `npx @auteric/cli` until
the package and corresponding API are published and validated together.

Auteric Kit is a coding-agent plugin for **custom commerce sites**. It helps a store team map its real catalog, prepare merchant-controlled UCP discovery, and check public exposure without moving checkout or payment away from the store. The kit contains skills, the executable connect CLI, the bundled connector SDK and read-only public verification tools. It does **not** contain a hosted Auteric Gateway, merchant account, signing key, or automatic runtime protection.

## Install in your store repository

Open your own storefront repository in the coding agent, then install:

| Agent | Command |
| --- | --- |
| Codex | `codex plugin marketplace add auteric-ai/auteric-kit && codex plugin add auteric-kit@auteric` |
| Claude Code | `claude plugin marketplace add auteric-ai/auteric-kit && claude plugin install auteric-kit@auteric` |
| Cursor | `npx skills add auteric-ai/auteric-kit --skill '*' --agent cursor` |

Alternatively download `auteric-kit.zip` from [Scanner onboarding](https://scanner.auteric.com/onboarding/), extract it in or beside your store project, and replace `auteric-ai/auteric-kit` in the command with the extracted `./auteric-kit` folder. Install only in a repository you trust; inspect the instructions before applying changes.

## Update an installed Codex plugin

Codex keeps a local marketplace snapshot. To receive a newer Auteric Kit release, refresh that snapshot before installing again:

```sh
codex plugin marketplace upgrade auteric
codex plugin add auteric-kit@auteric
```

## What happens after installation

Open Codex in the storefront repository. Auteric Kit surfaces the starter action **“Prepare this storefront for shopping agents with Auteric.”** Choose it to begin. You can also ask naturally, for example: “Prepare this store for shopping agents” or “Review this ecommerce site for AI shopping.” There is no magic skill name to learn.

Codex first inspects the repository and explains the smallest safe plan: catalog data, cart and checkout boundaries, likely files and routes, dependencies, validation, and any Auteric service requirement. Your connect request authorizes local installation, sign-in, adapters and sandbox tests. Approval is requested before push/publication/deployment. It makes the locally supportable changes, runs relevant checks, and reports exactly what still needs a merchant or Auteric operator.

Installing the plugin adds the capability only. It never changes storefront code, installs dependencies, creates credentials, or contacts an Auteric service by itself.

## Where a merchant runs Connect

Run the command from the repository root whenever possible. The CLI looks up to four
levels below it for a storefront and API service. With one candidate of each type it
chooses them automatically: the backend receives the connector and capability reports;
the frontend receives the prepared `/.well-known/ucp` document. If there are multiple
backend services, it stops before authentication and prints the candidates. Choose the
authoritative commerce service explicitly:

```sh
node /path/to/auteric-kit/bin/auteric.js connect \
  --localhost --store-url http://127.0.0.1:5500 \
  --backend services/commerce-api --frontend apps/storefront
```

The connector only maps APIs that it can trace and test. A route-name match, a button,
or browser `localStorage` is never enough to activate a merchant capability.

Codex, Claude Code, Cursor, and other Skills-compatible agents use the same inspected-and-approved workflow. Their buttons and commands differ by host; see [supported coding agents](COMPATIBILITY.md) for the matching entry point.

## What the merchant does

1. **Prepare:** The agent maps products, variants, price, availability, and canonical URLs from the real store. It implements only capabilities backed by existing business logic. Review the diff and run the store's tests.
2. **Connect:** An authorized merchant/operator creates a Store in a running Auteric Commerce service, provisions a scoped connector, activates tested mappings and policies, and obtains its signed UCP discovery document. The service is separate from this plugin. The kit never invents a Gateway URL, token, or attestation.
3. **Publish:** The merchant publishes the exact current service-issued JSON at `https://STORE/.well-known/ucp` on its own hostname. Existing UCP profiles must be reconciled; do not overwrite them blindly.
4. **Verify:** Run the read-only checker below, then [Scanner onboarding](https://scanner.auteric.com/onboarding/) → **Check my store**. A signed public declaration proves exposure only. Auteric protection additionally requires current, trusted Gateway and policy-enforcement evidence.

The [connection contract](plugins/auteric-kit/skills/auteric-connect/references/contract.md) details canonical catalog fields, signed exposure, and the separation between discovery and runtime protection. The [implementation workflow](plugins/auteric-kit/skills/auteric-connect/references/workflow.md) and [guided implementation](plugins/auteric-kit/skills/auteric-connect/references/implementation.md) define the review and approval gates.

## Read-only connection check

From the kit root:

```sh
python3 -m pip install -r requirements-verify.txt
python3 plugins/auteric-kit/skills/auteric-verify/scripts/check_connection.py --domain store.example
```

The first run reports the public UCP status. To verify an Auteric signature, supply an **independently trusted** key and expected key ID from the service operator:

```sh
python3 plugins/auteric-kit/skills/auteric-verify/scripts/check_connection.py \
  --domain store.example --public-key '<trusted-base64url-key>' --key-id '<expected-key-id>'
```

The checker never sends credentials, follows no redirects, executes no shopping actions, and always reports `enforcement_verified: false`. A `verified` exposure result is not a protected-status certificate. AI platforms decide discovery and placement; installation does not guarantee inclusion or sales.

## Development and release checks

```sh
python3 -m pip install -r requirements-verify.txt
python3 -m unittest discover -s tests -v
python3 scripts/package-openai-skills.py
```

The repository includes Codex and Claude Code plugin manifests and the `auteric-connect` and `auteric-verify` skills. The package script creates a skills-only ZIP for environments that accept a local plugin archive. See [release status](RELEASE_STATUS.md) before describing the kit as a live merchant connection.
