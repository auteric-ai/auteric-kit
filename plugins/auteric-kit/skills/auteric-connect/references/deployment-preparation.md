# Complete deployment preparation in the current Connect request

After local acceptance the CLI always writes owned `auteric/deployment-plan.json`.
`deployment_preparation_required` is a continuation for this same coding model,
not permission to finish at a successful local test. Read its blockers and resolve
every model-owned item before reporting. No second agent or merchant-specific
deployment generator is required.

1. Inspect the actual Dockerfile, startup, CI deploy workflow, task/Compose,
   proxy and persistence. A repository task can be historical. When authorized
   cloud credentials exist, use read-only `aws sts get-caller-identity`, ECS
   describe-services/describe-task-definition, EC2 describe-security-groups and
   describe-subnets, ALB describe-listeners/rules and Secrets Manager list/describe
   metadata. Do not print secret values. Confirm account/region/service against
   the requested store. Do not select a shared preview belonging to another
   installation or infer a task from an unrelated service. If access is missing,
   ask for login/access, not for a list of manually copied resource ARNs.
2. Reuse the real merchant database, command, roles, VPC, private subnets and
   deployment route. Check that business data survives task replacement. If a
   historical file uses `/tmp`, first inspect live state. An actual migration
   needs a backup, migration plan and owner's business-data decision. Never
   silently replace the merchant database to unblock Connect.
3. Put non-secret evidence in ignored `auteric/.state/deployment-context.json`.
   It can contain `platform`, `task_definition` (verified source path),
   `merchant_service`, `live_task_verified` and `deployment` (the existing pinned
   deployment/v1 schema). For ECS, the source must contain current durable
   merchant settings and valid healthcheck. A sanitized current task snapshot
   can live under ignored `.state`; generated final task must be reviewed for
   plaintext environment secrets before tracking. The context is installation
   evidence, not a commerce DSL. Determine dev/sandbox/staging/production from
   the requested target and continue internally with `--environment`.
4. For missing Auteric-owned ECS resources, supply `infrastructure` in that
   context: verified `vpc_id`, `subnet_ids` in at least two AZs,
   `merchant_security_group`, task/execution IAM role names, and either an
   existing runtime PostgreSQL URL `database_secret_ref` or `create_database:true`.
   The reusable Kit emits `auteric/infrastructure.json`: two private secrets,
   narrowly scoped role policies, and optionally an encrypted private PostgreSQL
   database. It never changes merchant business storage or the public service.
   Database creation incurs cost; present the concrete change and obtain the
   required cost/infrastructure authorization. Existing custom KMS secrets need
   separately verified decrypt permissions. Unsupported hosting needs its actual
   provider's template; do not pretend the ECS template applies everywhere.
5. When infrastructure execution is authorized, apply that owned CloudFormation
   template using the verified account/region, a unique installation stack name
   and CAPABILITY_NAMED_IAM. Use a change set for existing stacks. Retrieve only
   output references: EnrollmentSecret, ApplicationSecret, RuntimeDatabaseSecret.
   Do not copy values into repository files. Map outputs into deployment/v1
   `secret_ref`, `application_secret_ref`, and `state_ref.reference`. The empty
   enrollment secret is filled automatically by Connect after real Control login.
   Public merchant deployment remains a separate authorization.
6. Resume the original Connect internally with the completed evidence. It
   validates the deployment, opens Control login if a valid owner session is
   absent, enrolls the exact tested binding, and renders runtime connection,
   ECS task plus `auteric/ingress.json`, or the Compose overlay. Secrets are
   delivered through the existing private credential mechanism. The Sidecar
   image and Control/MCP code need no per-merchant rebuild or redeployment.
7. Complete reversible Dockerfile/workflow/proxy patches in the owned adapter
   plan. Feed the generated ECS task into the existing workflow, preserve image
   selection by merchant container name, and apply ingress intent to actual ALB
   rules. For Compose, reuse the existing network, create owned state volume if
   necessary and attach the merchant private token/runtime origin correctly.
   Preserve existing UCP integrations. Never substitute local test discovery.

Ask only for unavailable access, an ambiguous target, business-data migration,
required resource costs or actual deployment authorization. A user saying not to
deploy the store does not prevent preparing files; it does prevent publishing
the merchant. If infrastructure provisioning is also withheld, render its
template and explain that enrollment/final task await real stack outputs.
Report artifact preparation, infrastructure provisioning, enrollment, merchant
deployment and public verification separately. Never call a pending plan ready
for production. Disconnect removes unchanged owned files, not cloud resources
or merchant/runtime data; external resource cleanup is explicit and separate.
