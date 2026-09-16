# Guided implementation and approval

Use this guide after the initial repository inventory. It turns the merchant's natural request into a safe, reviewable integration.

## Plan format

Before writing, show a concise table:

| Area | Proposed local change | Evidence in this project | Status |
| --- | --- | --- | --- |
| Catalog | Exact read route/adapter and fields | Source file and model | Ready or needs decision |
| Cart | Existing safe handoff or no change | Source file and route | Ready, deferred, or unsupported |
| Checkout | Existing merchant checkout boundary | Source file and route | Handoff only unless connected tests exist |
| Discovery | `/.well-known/ucp` publication route | Framework/deployment route | Requires service-issued document if absent |
| Validation | Project checks and representative product cases | Existing scripts | Ready or unavailable |

Name every file expected to change and every dependency expected to be added. State that no protected status can be issued from local code alone. Wait for approval.

## Local integration rules

Use the project's framework and conventions; do not introduce a second server or generic mock API. A completed local implementation may include:

- a server-side catalog adapter backed by the real product/read model;
- read-only search and lookup routes with validation and bounded page sizes;
- the merchant application's `/.well-known/ucp` route, ready to serve a service-issued document;
- environment-variable names and an example file with no values or secrets;
- focused tests for product data and route behavior.

Do not write placeholder values for store IDs, gateway URLs, public keys, tokens, signatures, or credentials. If a project cannot host a route, explain the exact deployment configuration the merchant must provide instead of fabricating a file.

## Completion format

Report the result in this order:

1. **Completed locally** — source files, dependencies, routes, and validated behavior.
2. **Requires Auteric credentials/service** — only the exact account, configured service, or service-issued document still needed.
3. **Requires merchant/operator action** — approval, environment values, publishing, or production verification.
4. **Validation results** — commands, pass/fail state, and any item that could not be tested safely.

Do not collapse these states into “Auteric installed.”
