# Release status

Auteric Kit can be installed from this repository and used to prepare merchant storefront code. Its read-only verifier can inspect a published UCP document and verify a service-issued exposure signature with an independent trusted key.

Version 0.2.0 adds a user-facing Codex starter action and a mandatory inspect → plan → approval → implement → verify workflow. It does not use a SessionStart hook: the current official Codex documentation inspected for this release does not establish a supported SessionStart prompt-injection mechanism. The starter action and natural-language skill triggers provide discoverability without unsolicited session messages.

**Not yet demonstrated by this repository:** a fresh merchant installing the kit, deploying their own storefront changes, provisioning a production Auteric Commerce connection, publishing the signed document on their domain, passing connected action tests, and receiving current runtime-protection evidence. Those steps require a running HTTPS Gateway, merchant credentials and a merchant-controlled test store. The public Scanner alone cannot activate them.

Do not describe the kit as production protection, a one-click integration, or equivalent to a browser WebMCP tool SDK based on package validation alone. Nekuda's WebMCP Kit and this kit serve different protocols and verification boundaries. Compare them by actual merchant acceptance tests, not file count or marketing claims.

Acceptance evidence to collect before claiming a working merchant connection:

- exact kit commit and agent/install command;
- merchant repository, framework, reviewed diff, and passing store tests;
- HTTPS URL, HTTP 200, JSON content type and exact document at `/.well-known/ucp`;
- independently trusted exposure-signature verification;
- representative catalog search and lookup responses with price, variant and availability checks;
- current connector heartbeat, policy allow/deny test results, Gateway route, and Auteric runtime status;
- Scanner scan ID and timestamp showing the corresponding public and connected evidence.
