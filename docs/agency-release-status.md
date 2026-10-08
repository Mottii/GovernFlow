# Agency release implementation status

The [agency release brief](agency-release.md) supersedes the original PDF's delivery phases. Entries in `phase-exits.md` dated October 6 describe the original release and do not prove the agency release gates.

## Phase 1, foundation — 2026-10-08

Implemented in migration `0015` and the admin/gateway applications:

- Agencies, agency memberships, multiple explicit client assignments, external identity storage, and agency-bound admin sessions.
- Expand/backfill/enforce migration into the default agency. Existing client, workflow, user, virtual-key, password, and session identifiers remain intact. Legacy omitted-client grants become all-client access within that agency; explicit client grants stay explicit.
- Password-authenticated login chooses from active memberships. `agency_id` on login only selects an authenticated membership; arbitrary headers/query parameters cannot change authorization. Multiple memberships require an explicit selection. Roles and grants are re-read on every request. Membership revocation and agency suspension invalidate access immediately.
- Agency/client filtering on dashboard reads, spending aggregates, report exports, credentials, virtual keys, approvals, and mutations. Staff can have several assigned clients. Database composite foreign keys reject cross-agency assignments.
- Shared prices and the existing platform-wide emergency kill require platform maintenance authentication (`ADMIN_TOKEN`). Agency administrators cannot change other agencies' prices or issue a platform stop.
- Agency provisioning and operator-managed initial memberships. Users can subsequently administer their own agency's existing memberships. Last-administrator demotion is refused. Member deactivation is scoped to one agency.

Acceptance: 59 backend tests passed against an isolated Postgres 16, including the existing 200-call reservation race. New tests cover two agencies with two clients each, both directions of foreign resource selection, forged agency selectors, all report formats, multiple client assignments, grant changes on existing sessions, membership revocation, suspension, and database assignment constraints. A populated `0014` upgrade preserves identifiers/password hashes/key hashes and correctly backfills grants and sessions. Downgrading a multi-agency installation is explicitly refused. Ruff and mypy passed.

This is a foundation acceptance, **not the Phase 1 exit**. Reviewer groups, account-linked Telegram identities, scoped integration destinations, disabled legacy-destination import, and additional explicit agency ownership/composite foreign keys remain required. The next feature below supplies RLS, restricted-role support, and authenticated email review. The existing global notification/decision channels must not be used for a multi-agency deployment until that work passes acceptance. Phases 2–8 have not started under the new release brief.

## Administration compatibility

`POST /admin/users` preserves the existing `client_id` input. To assign several clients, supply `client_ids`; an explicit empty list grants no clients. `all_clients: true` grants access to all clients within the authenticated agency and cannot be combined with individual assignments. Omission of all scope fields preserves the old all-client behavior within that agency. Administrators require all-client access.

`POST /admin/auth/login` accepts optional `agency_id` after password verification. Existing single-membership callers need no changes. `/admin/auth/me` adds `agency_id` and `client_ids`; legacy `client_id` remains populated for exactly one assignment.

`POST /admin/agencies` and `PUT /admin/agencies/{id}/memberships/{user_id}` are platform provisioning operations. Ordinary sessions cannot use these paths to select another agency. `GET /admin/agency` reads the current agency; `PUT /admin/memberships/{user_id}` edits an existing membership in it. `DELETE /admin/users/{user_id}` now deactivates that agency membership and revokes its sessions, preserving access through other agencies.

`ADMIN_TOKEN` remains an existing compatibility mechanism; restricting it to initialization is part of Phase 7. Keep it operator-controlled. Direct SQL writes to the global user role/client columns no longer establish application permissions; membership and assignment resources are authoritative. Use migration `0015` for production upgrades; `Base.metadata.create_all` remains a test convenience.


## Phase 1, database boundary and authenticated review — 2026-10-08

Migration `0016` adds RLS to 21 tenant tables and scoped emergency-stop policies. Client ownership is propagated through existing workflow/approval foreign keys; reservation policies also require their budget and run to belong to the same workflow. Events, approvals, and alerts validate optional secondary ownership in their policies. Replay/webhook receipts gain agency ownership, with existing receipts backfilled into the default agency.

The migration creates a NOLOGIN `governflow_runtime` group with no superuser, bypass-RLS, role creation, replication, or database creation privileges, and rejects inherited database/table ownership. Grants are explicit, shared prices are read-only, and the runtime group receives no schema-creation privilege. Narrow SECURITY DEFINER functions authenticate hashed virtual keys/admin sessions and resolve memberships after password verification. Functions use qualified tables, fixed search paths, and no PUBLIC execute permission.

Authenticated agency context is transaction-local and restored when a session starts its next transaction. Unscoped access fails closed under the restricted role. New settlement sessions receive the key's authenticated agency explicitly; journal entries carry agency ownership. Worker entrypoints enumerate agencies and bind one context per job session. Old journal entries retain the default agency. The proxy now ends its read transaction before waiting on the provider, avoiding a second-session pool stall.

Backend review GET/POST now require an authenticated administrator or approver with current client access. A forwarded signed link grants no access, and decisions record the authenticated user instead of the link's channel name. The dashboard `/review/{id}` proxy uses its existing session cookie, forwards backend authentication, and rejects foreign-origin decisions. Configure `PUBLIC_DASHBOARD_URL` for notification links; Compose supplies its `DASHBOARD_PUBLIC_URL`. Direct backend review access requires a Bearer session.

As a transition safeguard, legacy global notifications are suppressed for new agencies and legacy Telegram/webhook callbacks can only reach the default agency. Default-agency channel behavior still awaits the requested explicit reviewer/destination migration. Dashboard review remains available in new agencies. This does not satisfy the complete channel-isolation exit.

Acceptance: all **63 backend tests** passed against Postgres 16. The new RLS acceptance uses an actual unprivileged login with a single-connection pool, covering 21 tenant tables, both agencies with two clients each, unscoped reads, cross-agency writes, mismatched reservation parents, read-only global prices, global-stop protection, context restoration and connection reuse, password/session/key authentication, and independent native-call settlements. Forwarded review links reject unauthenticated users, viewers, and foreign-client reviewers; valid reviewer identities are recorded. Signed legacy callbacks and global notification routing cannot act on a new agency. Ruff, mypy, JavaScript SDK tests, dashboard typecheck/build, Compose validation, and whitespace checks passed.

### Runtime deployment requirement

RLS is effective only with a login that does not own tables, inherit owner roles, or have superuser/BYPASSRLS privileges. The migration owner remains a separate operator identity and intentionally bypasses tenant policies for upgrades and provisioning. The existing Compose default still uses its legacy database owner; converting deployment initialization and secrets is pending packaging work. Do not treat that default as agency-safe deployment evidence.

After applying migrations as the owner, provision an application login through the operator's database tooling, for example:

```sql
CREATE ROLE governflow_app LOGIN NOSUPERUSER NOBYPASSRLS
    NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT governflow_runtime TO governflow_app;
-- Use the database tool's private password prompt / secret provisioning mechanism.
-- Never grant the owner role to this login.
```

Set every gateway, ordinary admin, and worker `DATABASE_URL` to that login. Keep platform agency creation and catalog maintenance on an operator-only instance using the migration identity; the runtime role intentionally cannot insert agencies or update the shared catalog. Platform membership provisioning validates operator authentication before binding the target agency. Migrations require permission to create/manage the NOLOGIN runtime group; managed database operators can pre-provision it with the documented restricted attributes.

`ENFORCE_AGENCY_RLS` defaults to true and requires the migrated authentication functions. Setting it false only supports metadata-created test databases and is not a supported release deployment mode. Tenant RLS policies remain enforced by Postgres regardless of this switch. Global local-account identities and the worker agency directory are shared system data; client content, credentials, approvals, and financial records use the tenant policies.

**Remaining Phase 1 work:** reviewer groups and explicit client/workflow assignments; Telegram account linking; separate destination credentials/service identities; import of legacy routing into disabled destinations; agency-specific price overrides; explicit ownership/composite constraints across all tenant records; deployment wiring to restricted logins; channel/worker/migration acceptance on that final design. Phases 2–8 remain pending, except for the short-transaction pool fix needed to verify the current boundary. The proxy's uncertain-charge lifecycle and replay reliability are still Phase 2 work.
