# GovernFlow Agency Release: Reliability, Client Governance, and Hosted Pilots

## Summary

Evolve GovernFlow into a practical governance service for automation agencies, with one shared codebase supporting:

- **Self-hosted deployment:** An agency operates its own instance for multiple clients.
- **Hosted deployment:** An invitation-only, free pilot on AWS ECS Fargate, managed Postgres, and S3.
- **Hosted authentication:** Amazon Cognito, verified email, and authenticator-app MFA for every user.
- **Business execution:** n8n or the client application performs approved actions. GovernFlow authorizes them and records reported outcomes.
- **Retention defaults:** Action content for 30 days, operational records for 180 days, and minimal audit evidence and financial records for 365 days.
- **Product focus:** Client-specific controls, dependable accounting, durable approvals, and evidence of execution.

Retain FastAPI, Postgres, Next.js, native provider dialects, integer micro-USD, UTC timestamps, and gateway-side token verification.

Execute the phases below in order. Use feature-sized commits, with acceptance evidence recorded before advancing to dependent work.

## Implementation phases

### 1. Establish agency isolation and client-specific approvals

- Add agencies, agency memberships, client assignments, external login identities, reviewer groups, and client-scoped integration destinations. Support staff assigned to several clients.
- Backfill the current installation into one default agency. Preserve existing identifiers, virtual keys, passwords, and explicit access grants.
- Derive agency context from authenticated membership or a workflow virtual key. A caller-supplied agency identifier must never establish authorization.
- Enforce agency isolation with Postgres row-level security and restricted runtime database roles. Continue enforcing client and workflow permissions in the application.
- Make the standard model-price catalog globally readable but writable only by platform maintenance. Agency-specific price overrides must remain agency-scoped.
- Replace global notification routing with explicit client/workflow reviewer assignments. Bind Telegram identities to users through account linking; check current assignments when decisions arrive.
- Require authenticated, authorized users for email review pages. A forwarded link alone must not grant access or decision authority.
- Give webhook integrations separate credentials and service identities. Record their configured identity rather than trusting a submitted approver name.
- Import legacy global notification settings as disabled destinations awaiting explicit assignment. Keep dashboard review available during migration.

**Exit:** Two agencies with two clients each cannot access, receive, or decide one another’s approvals through any supported channel.

### 2. Repair accounting and the proxy request lifecycle

- Introduce an explicit provider-call lifecycle: reserved, dispatched, settled with measured cost, settled with estimated cost, and voided. Store stable call IDs, reservation links, provider request IDs, dispatch timestamps, leases, and cost provenance.
- Persist dispatch intent before contacting a provider. Release money only when a request is known not to have been processed.
- Treat ambiguous timeouts, disconnects, and crashes after dispatch as uncertain charges. Settle a conservative estimate and display the uncertainty.
- Permit later measured evidence to correct an estimated charge through an idempotent ledger adjustment. Corrections must update budgets and reports without duplicating the original charge.
- Replace unconditional stale-reservation release: undispatched reservations may be voided; expired dispatched calls become estimated charges.
- Remove replay entries only after confirming durable settlement or a verified duplicate. Preserve failed entries for retry and inspection.
- Use Postgres for normal accounting and jobs. Use persistent local recovery storage for self-hosting and an S3 recovery journal for hosted settlement failures. Never depend on Fargate task-local storage for recovery.
- End database transactions before provider network waits. Reuse application-lifetime HTTP clients and use separate, short settlement transactions.
- Add request-size limits, bounded concurrency, stream deadlines, lease renewal, graceful draining, and explicit backpressure.
- Require or insert a configured native output limit before forwarding. Preserve explicit caller limits within policy. Continue rejecting unsupported billing categories.
- Keep hosted operation fail closed. Retain administrator-enabled R0/R1 fail open only in the documented single-node self-hosted profile with durable storage.

**Exit:** Timeout, crash, replay, and delayed-settlement scenarios never turn uncertain provider work into a silent zero charge or lose a queued measured charge.

### 3. Add agency-ready budgets, policies, and credential management

- Add shared client budgets above workflow and run budgets. Reserve against every configured level atomically through one accounting service with a consistent lock order.
- Preserve existing installations without forcing new client caps during migration. The onboarding wizard creates a client cap and initial workflow budget together.
- Add recurring UTC monthly budget templates. New wizard-created budgets renew automatically without carrying unused allowance forward; migrated budgets remain one-off until renewal is enabled.
- Apply run-cost and step-limit stops to the offending run by default. Client and workflow budget exhaustion block their respective scopes. Keep explicit emergency kill controls and configurable escalation.
- Record reservation overruns in full, emit alerts, and stop the affected run. Block wider scopes when their caps are exceeded.
- Add versioned policy editing, validation, previews, and rollback through a new immutable revision.
- Support a small deterministic condition set: field presence, equality, list membership, numeric thresholds, and recipient-domain restrictions. Use deny-first evaluation, then human review, then explicit allow; unmatched actions remain denied.
- Record the policy version and matching rules on decisions. Policy changes invalidate unconsumed authorizations issued under the previous version.
- Add provider credential replacement, rotation, retirement, and version identifiers. In-flight calls retain the version they started with.
- Version encryption and signing keys. Support controlled re-encryption and retain retired verification keys until previously issued tokens expire.
- Audit permission, policy, budget, integration, and credential changes.

**Exit:** Concurrent calls across multiple workflows share one client allowance; renewal is idempotent; policy and credential changes are reviewable and do not expose secrets.

### 4. Simplify onboarding and separate inference from reconciliation

- Add an onboarding wizard covering agency/client creation, an ordinary inference key, allowed models, spending limits, workflow creation, and one-time virtual-key delivery.
- Provide two explicit coverage modes:
  - **Gateway observed:** Native inference and governance work with an ordinary inference key.
  - **Provider reconciled:** Dedicated provider account mappings and reporting credentials enable independent comparisons.
- Preserve existing validated mappings and their strict settings. Reporting access becomes optional for new gateway-observed setups.
- Store reporting connections per agency/provider organization, supporting clients that own separate organizations. Associate each mapped project/workspace with the correct connection.
- Require dedicated provider scopes for reconciliation and validate them using that connection’s credentials.
- Show reporting coverage, last successful refresh, failures, estimates, and unattributed differences. Never infer individual call charges from aggregate provider totals.
- Keep mock onboarding available without paid credentials. Disable unconfigured notification channels rather than generating avoidable delivery failures.
- Add pagination, searchable lists, integration health, and maintenance controls to the dashboard.

**Exit:** A user can make a governed live call with an ordinary inference key without first obtaining an organization reporting key.

### 5. Make approval workflows durable and integrate n8n

- Extend action checks with a stable request ID and run reference. Repeating the same request returns the existing approval; reusing the ID with different action content returns a conflict.
- Add a durable decision-event outbox. Deliver approval, denial, and expiry callbacks with stable event IDs, bounded retries, and visible failed-delivery handling.
- Restrict callbacks to registered integration destinations with authentication and SSRF protection. Private-network destinations require explicit self-hosted configuration.
- Support approvals that remain valid until their configured deadline. Issue short-lived execution tokens when the workflow resumes, bounded by that deadline. Reading approval status must not extend the approval deadline.
- Preserve atomic single-use consumption. A lost consumption response or crash after consumption must not trigger automatic re-execution.
- Create an action-attempt record on consumption and accept idempotent outcome receipts: succeeded, failed, or unknown. Mark them as orchestrator-reported evidence.
- Add explicit run start, completion, failure, and cancellation. Correlate native calls through the existing `X-Run-Id`.
- Update Python and JavaScript SDKs with durable identifiers, resume/status helpers, typed errors, and outcome reporting. Keep existing methods available.
- Ship an importable n8n workflow using authenticated Wait/resume behavior, authoritative approval lookup, token consumption, and result reporting.
- Build an n8n package for run management, native model calls with run IDs, approval waits, consumption, and outcome reporting. Prepare community verification materials; availability through n8n’s verified directory remains an external dependency.
- Include example workflows for client email review and CRM updates, initially using mock business actions.

**Exit:** Restarting a workflow while awaiting approval preserves the request and allows safe continuation. Duplicate callbacks do not cause duplicate authorization or automatic repetition of an uncertain business action.

### 6. Add execution visibility, useful reports, and verifiable retention

- Build a run timeline joining model calls, policy decisions, approval waits, consumption, outcomes, corrections, and source execution links.
- Extend reports with completed and failed runs, unresolved outcomes, approval turnaround, estimated charges, and cost per reported successful run. Keep manually supplied savings clearly labeled.
- Add OpenTelemetry tracing and low-cardinality metrics for latency, errors, database pressure, concurrency, replay backlog, callback delivery, and reconciliation freshness.
- Keep prompt bodies, action payloads, credentials, and raw provider error content out of telemetry.
- Separate liveness from readiness. Readiness checks required database/schema/configuration dependencies; worker health includes job progress and backlog.
- Implement daily signed audit checkpoints containing chain boundaries, hashes, counts, and the previous checkpoint reference. Store no action content or reviewer email addresses in checkpoints.
- Store checkpoints in a versioned S3-compatible bucket with Object Lock. Use a separate signing identity; application runtime roles receive no checkpoint deletion or retention-bypass permissions.
- Use AWS KMS signing for hosted checkpoints and a separate local signing key for self-hosting. Publish verification keys and provide an independent verification command.
- Segment audit history so expired records can be removed while retained history remains verifiable against trusted external checkpoints.
- Implement the selected retention periods, including encrypted content removal, operational cleanup, financial/evidence expiration, and backup-expiry documentation.
- Provide retention previews and deletion evidence. Run the first upgrade purge in preview mode; activate scheduled deletion after verifying the migration’s retention classification.
- Make self-hosted audit-storage capability visible. An endpoint without Object Lock must be labeled as lacking enforced immutable storage.

**Exit:** Altering or truncating retained audit history is detected; expired content disappears from normal access; retention does not silently invalidate the remaining chain.

### 7. Package both editions and deploy the hosted pilot

- Publish versioned backend and dashboard images, including supported CPU architectures, release notes, configuration examples, and reproducible builds.
- Provide a self-hosted Compose release with generated local secrets, initialization/migration commands, a first-admin flow, persistent recovery storage, and optional mock services.
- Consolidate background scheduling behind a worker entrypoint using durable jobs, leases, and retry state. Allow multiple workers without duplicate effects.
- Supply Terraform for ECS Fargate, RDS Postgres 16, ALB/TLS, S3, Cognito, KMS, Secrets Manager, logging, monitoring, and backups.
- Keep gateway and admin applications separate. Restrict database and internal administrative connectivity; expose only intended authenticated interfaces and callbacks.
- Use Cognito managed login with verified email and required TOTP MFA. Do not grant application access before onboarding requirements are complete, including first-login edge cases.
- Keep local accounts for self-hosting, adding invitations and password recovery when email delivery is configured. Restrict bootstrap tokens to initialization.
- Start hosted access through operator-created agency invitations. Include agency suspension and resource limits; omit public signup and subscription billing.
- Add immutable-image deployment, migration compatibility checks, rollback procedures, restore exercises, secret rotation, and operational runbooks.
- Configure streaming and load-balancer timeouts together. Durable recovery must survive replacement of any application task.

AWS-specific implementation must follow the documented behavior of [Fargate ephemeral storage](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-storage.html), [S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html), and [Cognito MFA](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-mfa.html).

**Exit:** A clean self-hosted installation and the AWS staging environment pass the same functional acceptance suite.

### 8. Complete live verification and validate agency demand

- Run bounded live contracts against OpenAI, Anthropic, and Gemini, including streaming and supported usage accounting. Verify current provider formats and prices before capturing sanitized fixtures.
- Verify real Telegram, email, webhook delivery, dedicated provider mappings, and delayed reporting reconciliation.
- Exercise the complete scenario: two clients, different reviewers, a restarted n8n workflow, isolated permissions, corrected accounting, and a client report.
- Prepare an interview guide, pilot onboarding material, feedback form, and reproducible issue-report template.
- Recruit three agencies through user-led outreach. Establish their current approval, spending, and reporting problems before evaluating the product.
- Measure onboarding time, weekly active agencies, governed workflow completion, approval turnaround, unresolved charges, and reporting usefulness.
- Publish engineering acceptance separately from pilot adoption results. Do not describe demand or production readiness as proven by mock tests.

## Public interfaces and migration strategy

| Area | Additions or changes |
|---|---|
| Agency administration | Agency membership, client assignment, invitation, integration-destination, and reviewer-group resources |
| Run lifecycle | `POST /v1/runs`, `GET /v1/runs/{id}`, and `PATCH /v1/runs/{id}`; stable external execution IDs |
| Action checks | Optional `request_id`, run reference, and registered callback reference on `/v1/actions/check` |
| Consumption and outcomes | Preserve `/v1/actions/consume`; return an attempt identifier; add `POST /v1/actions/{id}/outcome` |
| Policies | `PUT /admin/workflows/{id}/policy` with version preconditions; `POST /admin/policies/preview`; revision history and activation |
| Budgets | Client budgets, recurring budget templates, and audited cap updates |
| Credentials | Credential version/rotation operations and agency-scoped reporting connections |
| Operational views | Paginated run, attempt, delivery, recovery, and integration-health views |
| Health and telemetry | Separate liveness/readiness endpoints and configurable OTLP export |
| Audit and retention | Checkpoint verification, retention preview, and purge-history interfaces |

Start new Alembic migrations after `0014`; do not rewrite existing migrations. Use expand, backfill, validate, and then enforce constraints. Add agency ownership and matching foreign-key constraints to tenant-owned records.

Preserve native proxy paths and provider envelopes. Preserve existing action methods and list response bodies where possible; pagination can expose continuation through response headers. Document changed approval-expiry and stop-scope behavior explicitly.

Legacy global access becomes explicit access within the default agency. Legacy encryption and audit formats remain readable during migration. Existing audit events are sealed into a legacy segment before new segmentation begins.

## Verification and release gates

**Accounting and recovery**

- Retain the 200-call reservation race; extend it across workflows sharing a client cap.
- Cover mixed reserve/settle/correct operations, month rollover, concurrent cap changes, and repeated replay.
- Crash before dispatch, after dispatch, after provider success, and during settlement.
- Reap before replay and replay before reaping; verify identical financial results.
- Confirm late measured charges correct estimates exactly once.

**Isolation and approvals**

- Test agency and client isolation on every new and existing interface, callback, export, and background job.
- Run database tests using restricted runtime roles to verify row-level security.
- Test reviewer removal, forwarded email links, wrong Telegram identities, forged callbacks, changed action content, policy changes, expiry, and concurrent consumption.
- Verify uncertain execution outcomes never cause an automatic business-action retry.

**Performance and operations**

- Test at least 100 concurrent slow streams with deliberately constrained database pools.
- Run a documented reference benchmark with a mock provider, measuring gateway overhead, first-byte delay, memory, and connection usage.
- Target p95 gateway-added latency below 100 ms at 20 requests/second on the documented reference deployment; treat this as an acceptance target, not a public SLA.
- Exercise task replacement, unavailable Postgres, unavailable S3, worker restart, key rotation, and backup restoration.

**End-to-end release**

- Run backend, SDK, n8n, browser, migration, secret-scanning, and container checks in CI.
- Verify fresh installation and upgrade from the current release.
- Require a successful two-client workflow scenario on both deployment editions.
- Complete live external checks before calling the hosted pilot ready.
- Target first-workflow setup within 15 minutes after deployment and weekly continued use by at least two pilot agencies without developer intervention.

## Assumptions and external dependencies

- The release remains open source under the existing MIT license.
- Hosted launch is an invited free pilot; payment processing and public self-service signup are deferred.
- Agencies supply their own inference credentials. Reporting credentials are optional unless reconciliation is enabled.
- AWS infrastructure defaults to `eu-central-1`, with region and owned domains supplied as deployment inputs.
- S3 checkpoints use governance retention by default, with application roles unable to bypass it. Retention configuration is explicit and does not constitute a compliance certification.
- Sensitive business actions remain in n8n/client applications. Execution receipts record their reported results; GovernFlow does not promise exactly-once external execution.
- Additional provider families, a general-purpose MCP gateway, enterprise SSO for self-hosting, and Kubernetes packaging are deferred until pilot evidence justifies them.
- AWS access, domains, email delivery, live provider credentials, notification credentials, and pilot participants are required for their respective deployment and live acceptance gates. Their absence must remain visible in release status rather than being replaced by a mock pass.
