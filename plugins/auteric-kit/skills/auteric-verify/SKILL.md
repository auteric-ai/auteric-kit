---
name: auteric-verify
description: Verify an Auteric storefront's public UCP profile and signed exposure using an independently pinned key, then distinguish discovery from runtime enforcement. Use after connection or publication and when diagnosing a failed Auteric check.
---

# Verify a connection

Run this skill's `scripts/check_connection.py --domain DOMAIN` to read the target merchant's public `https://DOMAIN/.well-known/ucp` without sending credentials or following redirects. Report its HTTP status and any challenge separately. If blocked, continue with available public catalog evidence; do not label a challenge page as absent support or bypass access controls.

When the operator provides an independently trusted Auteric key, pass `--public-key` and `--key-id` to `check_connection.py`. Never obtain the trusted key from the profile being checked. The helper returns a machine-readable exposure result; exit 2 means invalid or not yet verified. For an already saved JSON document, use `verify_profile.py PROFILE.json --domain DOMAIN` with the same key options. Neither helper executes shopping writes.

Then verify the declared catalog read routes with representative products and pagination, recording sampled coverage. Compare the signed endpoint/capabilities with actual behavior. Do not execute a purchase or payment as a connectivity test. Use merchant-authorized test resources for connected actions.

Return to the Scanner connection check. Public signature verification and connected action tests are distinct from trusted runtime enforcement. A successful helper result leaves `enforcement_verified: false`; only the service's authenticated runtime evidence can establish protection. Explain any missing dependency and next step precisely. Never report a local fixture, a copied prompt, or a declaration as a live verified connection.
