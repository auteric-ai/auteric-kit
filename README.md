# Auteric Kit

Auteric Kit is a coding-agent plugin for **custom commerce sites**. It helps a store team map its real catalog, prepare merchant-controlled UCP discovery, and check public exposure without moving checkout or payment away from the store. The kit contains instructions and read-only verification tools. It does **not** contain a hosted Auteric Gateway, merchant account, signing key, or automatic runtime protection.

## Install in your store repository

Open your own storefront repository in the coding agent, then install:

| Agent | Command |
| --- | --- |
| Codex | `codex plugin marketplace add auteric-ai/auteric-kit && codex plugin add auteric-kit` |
| Claude Code | `claude plugin marketplace add auteric-ai/auteric-kit && claude plugin install auteric-kit@auteric` |
| Cursor | `npx skills add auteric-ai/auteric-kit --skill '*' --agent cursor` |

Alternatively download `auteric-kit.zip` from [Scanner onboarding](https://scanner.auteric.com/onboarding/), extract it in or beside your store project, and replace `auteric-ai/auteric-kit` in the command with the extracted `./auteric-kit` folder. Install only in a repository you trust; inspect the instructions before applying changes.

Ask the agent:

> Use `auteric-connect` to prepare this custom storefront for Auteric. Inspect the existing product, inventory, cart, and checkout code. Plan the smallest changes, implement accurate catalog discovery using the store's own data, and show the tests and publication steps. Do not claim Auteric protection until the connected runtime check passes.

## What the merchant does

1. **Prepare:** The agent maps products, variants, price, availability, and canonical URLs from the real store. It implements only capabilities backed by existing business logic. Review the diff and run the store's tests.
2. **Connect:** An authorized merchant/operator creates a Store in a running Auteric Commerce service, provisions a scoped connector, activates tested mappings and policies, and obtains its signed UCP discovery document. The service is separate from this plugin. The kit never invents a Gateway URL, token, or attestation.
3. **Publish:** The merchant publishes the exact current service-issued JSON at `https://STORE/.well-known/ucp` on its own hostname. Existing UCP profiles must be reconciled; do not overwrite them blindly.
4. **Verify:** Run the read-only checker below, then [Scanner onboarding](https://scanner.auteric.com/onboarding/) → **Check my store**. A signed public declaration proves exposure only. Auteric protection additionally requires current, trusted Gateway and policy-enforcement evidence.

The [connection contract](plugins/auteric-kit/skills/auteric-connect/references/contract.md) details canonical catalog fields, signed exposure, and the separation between discovery and runtime protection. The [implementation workflow](plugins/auteric-kit/skills/auteric-connect/references/workflow.md) gives the agent review and acceptance gates.

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
