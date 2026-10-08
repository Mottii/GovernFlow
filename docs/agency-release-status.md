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

This is a foundation acceptance, **not the Phase 1 exit**. Database RLS/restricted runtime roles, ownership constraints for the rest of the tenant records, reviewer groups, account-linked Telegram identities, scoped integration destinations, legacy-channel migration, and authenticated email review remain required. The existing global notification/decision channels must not be used for a multi-agency deployment until that work passes acceptance. Phases 2–8 have not started under the new release brief.

## Administration compatibility

`POST /admin/users` preserves the existing `client_id` input. To assign several clients, supply `client_ids`; an explicit empty list grants no clients. `all_clients: true` grants access to all clients within the authenticated agency and cannot be combined with individual assignments. Omission of all scope fields preserves the old all-client behavior within that agency. Administrators require all-client access.

`POST /admin/auth/login` accepts optional `agency_id` after password verification. Existing single-membership callers need no changes. `/admin/auth/me` adds `agency_id` and `client_ids`; legacy `client_id` remains populated for exactly one assignment.

`POST /admin/agencies` and `PUT /admin/agencies/{id}/memberships/{user_id}` are platform provisioning operations. Ordinary sessions cannot use these paths to select another agency. `GET /admin/agency` reads the current agency; `PUT /admin/memberships/{user_id}` edits an existing membership in it. `DELETE /admin/users/{user_id}` now deactivates that agency membership and revokes its sessions, preserving access through other agencies.

`ADMIN_TOKEN` remains an existing compatibility mechanism; restricting it to initialization is part of Phase 7. Keep it operator-controlled. Direct SQL writes to the global user role/client columns no longer establish application permissions; membership and assignment resources are authoritative. Use migration `0015` for production upgrades; `Base.metadata.create_all` remains a test convenience.
