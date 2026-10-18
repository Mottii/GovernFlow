# Agency release implementation status

The [agency release brief](agency-release.md) supersedes the original PDF’s delivery phases. Entries in `phase-exits.md` dated October 6 refer to the original release.

## Phase 1 complete — 2026-10-15

Agency isolation and client-specific approvals satisfy the Phase 1 engineering exit: two agencies, each with two clients, cannot access, receive, or decide one another’s approvals through the supported channels.

Implemented in migrations `0015`–`0018` and the applications:

- Agencies, active memberships, multiple explicit client assignments, external identities, and agency-bound sessions. Login selects only password-verified memberships. Roles, client grants, and membership state are rechecked on requests; agency suspension blocks authentication.
- Explicit agency ownership and composite parent foreign keys for tenant records. Legacy identifiers, virtual keys, password hashes, sessions, and explicit grants survive the populated upgrade. Existing orphan audit client identifiers are preserved through archived recovery clients; audit payloads are unchanged. Unsafe multi-agency downgrade is refused.
- Postgres RLS on existing tenant tables and the six new reviewer/destination/override tables. Unscoped access fails closed. Transaction-local agency context is restored across commits and pooled connection reuse. Separate settlement transactions and worker sessions receive authenticated agency context.
- Client-bound reviewer groups, current group membership, and client/workflow assignments. Assigned approvers can review only their authorized workflows; agency administrators retain scoped dashboard recovery authority. Permission administration serializes with decision authorization.
- Explicit client/workflow email, Telegram, and webhook destinations. Queued deliveries recheck assignments before sending. Assigned clients cannot be changed on an existing destination, keeping old integration credentials from moving to another client.
- Personal, expiring, single-use Telegram account-link challenges. Callbacks require the bot transport secret, chat-bound signature, linked account, current membership/client access, and matching active reviewer assignment and destination. Wrong users and removed reviewers are rejected.
- Authenticated email review GET/POST and dashboard review. Forwarded signed links do not confer access; authenticated reviewer identity is recorded. The dashboard supports agency selection after password verification and proxies PUT administration requests.
- Separate webhook destination credentials encrypted at rest, configured service identities, timestamped HMAC verification, scope checks, and replay rejection. Submitted approver names are ignored. The old global decision endpoint returns 410.
- Legacy environment routing is imported as encrypted, disabled destinations awaiting explicit assignment; pending legacy deliveries are cancelled. Existing reviewer grants become explicit groups so dashboard review remains available. Global routing settings no longer deliver notifications or authorize decisions.
- Shared prices remain globally readable and maintenance-only for writes. Validated effective-dated agency overrides affect gateway pricing and model discovery within that agency.
- Compose initialization uses the database owner for migrations, catalog seeding, and restricted login provisioning. Gateway, ordinary admin, and all workers use `governflow_app`, inheriting only the restricted runtime group. Application connections refuse owner, superuser, bypass-RLS, and role-administrator logins. Optional platform maintenance is confined to a separate profile bound to localhost.

### Acceptance evidence

- **67 backend tests passed** against two disposable Postgres 16 databases, including the existing 200-call reservation race. Five restricted-login tests cover existing tenant tables and new resources, authentication, cross-agency writes, parent constraints, native settlements, transaction/pool reuse, scoped delivery, valid/foreign human decisions, reviewer removal, webhook identity/scope, price overrides, and isolated expiry/reaping workers.
- Populated `0014` → head upgrade preserves identifiers, key/password hashes, sessions, and client grants; imports three encrypted disabled routes; backfills explicit reviewer membership. Fresh upgrade/downgrade and append-only audit enforcement pass.
- Ruff, strict backend mypy, Python SDK mypy, and JavaScript SDK tests pass. Dashboard TypeScript checks and production build pass. Compose configuration validates.
- A fresh isolated Compose stack builds and initializes without manual migration commands. An ordinary application container reports `current_user = governflow_app` and sees zero clients without agency context. Gateway, admin, notifier, reaper/replay, and reconciler processes run with the restricted configuration.
- **Two Playwright tests passed** against the Compose stack: signed-in client workflow, key creation, native mock call, visible spend, stop propagation, mobile controls/sign-out, and foreign-origin mutation rejection.

Provider, Telegram, SMTP, and webhook transports were mocked for engineering acceptance. Real external delivery/provider verification remains Phase 8; AWS hosted deployment and Cognito remain Phase 7. These results do not establish live pilot adoption or production readiness.

## Administration and upgrade notes

See [agency administration](agency-administration.md) for reviewer/destination setup, account linking, webhook signatures, price overrides, and database identity separation. Add a generated URL-safe `APP_DATABASE_PASSWORD` to an existing private environment before upgrading; preserve the existing owner password and encryption/signing keys.

`POST /admin/users` retains legacy `client_id`. Use `client_ids` for several clients, an empty list for no clients, or `all_clients: true` for the whole agency. Administrators require all-client access. Omitted scope fields preserve legacy all-client behavior inside the authenticated agency.

`POST /admin/auth/login` accepts an optional `agency_id` only as a selector among verified memberships; several memberships require explicit selection. `/admin/auth/me` includes `agency_id` and `client_ids`.

Agency creation and initial membership provisioning remain operator operations. The existing `ADMIN_TOKEN` supports default-agency first-admin creation; restricting bootstrap tokens to initialization is part of Phase 7. Shared catalog and cross-agency provisioning require the separate maintenance process. `ENFORCE_AGENCY_RLS=false` supports metadata-created tests only.

## Next work

Phase 3 adds shared client budgets, recurring UTC budgets, scoped run stops, versioned policies, and credential/key rotation. Phases 3–8 remain pending under the agency release brief. AWS deployment, Cognito, live external transport/provider verification, and hosted pilot acceptance remain later gates.

## Phase 2 complete — 2026-10-18

Provider calls now have durable dispatch intent, explicit lifecycle states, leases, price snapshots, and measured/estimated provenance. Migration `0019` adds canonical calls, append-only charge adjustments and evidence receipts, and leased recovery jobs under agency RLS. Live settlement, expiry, legacy replay, and administrator corrections use one transactional accounting path. Unknown post-dispatch outcomes become conservative estimates, while later measured evidence adjusts the original call and UTC budget period exactly once. Workers never repeat inference.

Persistent SQLite supports self-hosting; hosted mode requires an S3 journal and always fails closed. Conditional journal creation, checksum-bound receipts, and acknowledgement after commit preserve evidence through duplicate replay and lost acknowledgements. Failed evidence remains retryable or quarantined. Explicit fail open is limited to administrator-enabled R0/R1 workflows in a single-node SQLite deployment. If terminal journal writes fail, intake blocks while evidence retries; a hard loss during simultaneous database/journal failure retains the dispatch estimate but can lose the in-memory measurement.

The proxy reuses application-lifetime HTTP clients, enforces provider-native output limits, bounds requests/responses/parser buffers and concurrency, renews dispatch leases, and shields cancellation finalization. Provider waits hold no database transaction. Scoped call/recovery inspection, explicit retries, and revision-checked evidence-backed administrator corrections are available through the admin API. Reports and existing dashboard views show estimates, corrections, and imported recovery backlog without double-counting charges.

### Acceptance evidence

- **92 backend tests passed** against two disposable Postgres 16 databases and a Moto 5.2.4 S3 emulator. Coverage includes the existing 200-call reservation race, mixed reserve/settle/reap/measurement/cap changes, both reaper/replay orders, late measurements, UTC original-month corrections, duplicate/conflicting evidence, ten concurrent recovery workers, lost acknowledgements, corrupt S3 retention, persistent journal replacement, legacy upgrade, cancellation, and temporary terminal-storage failure without repeated inference.
- **100 simultaneous slow streams** complete with a database pool of five connections. The test confirms zero checked-out database connections while all streams wait for provider data, then verifies measured accounting.
- Populated `0018` → `0019` upgrade preserves pre-existing balances and historical audit payloads. Legacy pending dispatches recover conservatively; late usage adjusts their original month. Downgrade after accounting activity is refused. Restricted-role tests verify agency/client isolation, scoped corrections/recovery, parent constraints, and append-only ledger/receipts.
- Ruff, strict backend/Python SDK mypy, JavaScript SDK tests, dashboard TypeScript checks and production build, Compose configuration/build/initialization, and redacted source/history secret scans pass.
- **Two Playwright tests passed** against the isolated Compose stack: authenticated workflow/key/native call/spend/stop/mobile/sign-out flow and cross-origin mutation rejection. Provider and transport fixtures are deterministic mocks.
- Final local mock benchmark: **200 requests at 20 rps**, direct-provider p95 **3.341 ms**, gateway p95 **31.634 ms**, paired added-latency p50 **16.904 ms** and p95 **30.427 ms**, below the **100 ms** target. Environment: macOS 26.6.2 arm64 client, Docker aarch64 with 10 CPUs and 7.75 GiB memory, Postgres 16, default gateway concurrency 128 and pool 10. Gateway memory after the run was approximately 79 MiB. This is a development benchmark, not a hosted capacity claim.

See [provider accounting and recovery](phase2-accounting.md) for the implementation sequence, APIs, defaults, S3 permissions, durability boundaries, and maintenance-window upgrade/rollback procedure. S3 emulator acceptance does not establish actual AWS IAM, bucket-policy, or task-replacement correctness; those require hosted acceptance in Phase 7. Live provider charges and external notification delivery remain Phase 8.

## Prior milestones

- 2026-10-08: Agency membership/client-assignment foundation accepted with 59 backend tests (`52d7f49`).
- 2026-10-08: Initial RLS boundary and authenticated review accepted with 63 backend tests (`9897ae2`). Restricted-role deployment wiring and scoped channel authorization were still pending at that milestone; the Phase 1 exit above supersedes those limitations.
