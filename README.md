# Auteric Kit

## CLI preview for custom storefronts

The repository now includes a Node.js CLI source package. It has **not** been
published to npm. Until the control-plane authentication routes are running,
use the local checkout to inspect a store without changing it:

```sh
node /path/to/auteric-kit/bin/auteric.js connect --domain store.example.com --dry-run
```

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
a pending store only after you approve the session. Neither a password nor the
short-lived session token is stored in the storefront repository. It writes
`.auteric/config.json` with non-secret local IDs and state.

When the authenticated Commerce API returns a signed UCP document, the CLI asks
before preparing `/.well-known/ucp` in the correct static/public directory.
It refuses to replace an existing, different profile. For a plain static site
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

This is an **incomplete vertical slice**: the CLI identifies available agents
but does not install or invoke them, map or test commerce capabilities, provision
MCP, or enable runtime protection. `disconnect` fails explicitly until revocation
is implemented. The local Commerce API requires the separate
`auteric-commerce-starter/src` and `auteric-commerce-sdk/src` packages on
`PYTHONPATH` (or installed in the server virtual environment). Do not advertise `npx @auteric/cli`
until the package and corresponding API are published and validated together.

Auteric Kit is a coding-agent plugin for **custom commerce sites**. It helps a store team map its real catalog, prepare merchant-controlled UCP discovery, and check public exposure without moving checkout or payment away from the store. The kit contains instructions and read-only verification tools. It does **not** contain a hosted Auteric Gateway, merchant account, signing key, or automatic runtime protection.

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

Codex first inspects the repository and explains the smallest safe plan: catalog data, cart and checkout boundaries, likely files and routes, dependencies, validation, and any Auteric service requirement. It waits for your normal approval before modifying application files. After approval, it makes only the locally supportable changes, runs relevant checks, and reports exactly what still needs a merchant or Auteric operator.

Installing the plugin adds the capability only. It never changes storefront code, installs dependencies, creates credentials, or contacts an Auteric service by itself.

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
