# Deployment preparation and owner authorization

Run canonical local acceptance before enrollment. A passing local test does not
activate public traffic. Connect then opens Control authorization immediately,
before cloud infrastructure preparation. `authentication_pending` is a resumable
boundary: show the owner the URL and expiry, end the model turn, and continue the
same Connect request after they sign in. Control must verify PKCE approval;
a user message alone is not authorization. Expired authorization can restart
without regenerating the adapter or discarding passing, unchanged local work.

The generic runtime uses `state_ref.profile: "gateway/v1"`. Execution claims,
validated receipts, nonce replay protection, audit, identity mappings and bridge
session references live in Auteric's existing authenticated storage service.
Do not create a merchant-side runtime database, RDS, storage volume, or inject a
merchant database URL into the runtime. Do not propose PostgreSQL as an onboarding
prerequisite. Gateway unavailability fails closed; it never falls back to local
state or blindly repeats an uncertain merchant write.

1. Inspect Dockerfile, startup, deployment workflow, task/Compose, proxy, existing
   credentials and business persistence. When authorized, inspect the actual
   cloud account/region/service read-only. Historical source files are not proof
   of live configuration. Ask for missing access rather than manually copied ARNs.
2. Preserve the merchant command, auth, transactions, database and existing UCP.
   Business persistence remains merchant-owned. A genuine business-data migration
   requires its own backup, plan and owner decision; Connect cannot silently
   replace that database. It is separate from Auteric runtime storage.
3. Put verified deployment references under ignored
   `auteric/.state/deployment-context.json`: `live_task_verified`, `deployment`,
   and environment evidence. Use the immutable qualified runtime image, existing
   network/task source, `gateway/v1`, and installation-scoped secret references.
4. Secrets are still needed: installation identity authenticates the runtime to
   Auteric; the private application token authenticates the bridge to the merchant
   hook. Reuse suitable installation-owned secret infrastructure. For missing ECS
   secrets, `infrastructure` needs only verified task/execution role names. The
   Kit emits two Secrets Manager entries and narrowly scoped IAM policies, with
   no database, disk, VPC, subnets or business resource changes. Provision only
   when authorized; secret services may have costs. Inspect KMS permissions when
   applicable. Unsupported platforms need their actual provider's secret mechanism.
5. Enrollment uses the real owner's verified Control session and exact tested
   adapter. It issues installation identifiers and limited enrollment credentials;
   do not invent merchant IDs or copy development credentials. Deliver through
   platform secrets, never source code. Existing identities must be inspected,
   not silently replaced or revoked.
6. Render the runtime connection, ECS task/ingress intent or Compose overlay, and
   connect those artifacts to the merchant's existing deployment workflow. Keep
   the bridge private. ECS uses Secrets Manager for credential rotation and only
   task-local immutable adapter/discovery volumes. Compose uses temporary runtime
   state and a dedicated private `auteric/.state/identity/` directory for rotating
   installation credentials; this stores secrets, not an execution database.
   Its runtime UID must own that directory. Application secrets remain read-only.
7. Prepare the merchant discovery route/proxy before SPA fallback. It serves exact
   Control-issued bytes from the runtime and fails closed when unavailable. Cloud
   deployment and public Connection Test require separate deployment authorization.

`deployment_preparation_required` is a continuation for the same coding model.
Resolve model-owned blockers and prepare all reversible files before requesting
missing access, target clarification or deployment authorization. Never introduce
an unrelated live preview as the target. No per-merchant Sidecar rebuild or
Control/MCP redeployment is needed.

Report local tests, owner authentication, enrollment, artifact preparation,
merchant deployment and public verification separately. Disconnect revokes the
installation and removes unchanged owned files. It preserves merchant data,
edited files and central audit; cloud resource cleanup is explicit and separate.
