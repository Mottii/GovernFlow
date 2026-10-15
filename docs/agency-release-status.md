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

Phase 2 begins with the explicit provider-call lifecycle, dispatch intent, uncertain-charge accounting, correction ledger, replay durability, and bounded proxy concurrency. Phases 2–8 remain pending under the agency release brief. The earlier short-transaction pool fix is a supporting change, not completion of Phase 2.

## Prior milestones

- 2026-10-08: Agency membership/client-assignment foundation accepted with 59 backend tests (`52d7f49`).
- 2026-10-08: Initial RLS boundary and authenticated review accepted with 63 backend tests (`9897ae2`). Restricted-role deployment wiring and scoped channel authorization were still pending at that milestone; the Phase 1 exit above supersedes those limitations.
