# Dashboard

The Next.js dashboard serves port 3000 and calls the separate admin API through server-side route handlers. Login stores the session token in an HttpOnly, SameSite Strict cookie. Mutations require the dashboard's exact origin. Set `DASHBOARD_PUBLIC_URL` to your deployment's HTTPS origin; HTTPS enables Secure cookies. Session tokens are hashed in Postgres, expire after eight hours, and are revoked on sign out or account disable. Passwords use salted scrypt. Login attempts are limited in Postgres across replicas.

## First account

Set `DASHBOARD_ADMIN_EMAIL` and a password of at least 12 characters in the gitignored `.env`, then run `uv run python scripts/bootstrap_admin.py` after migrations and the admin service are ready. The script authenticates with the bootstrap `ADMIN_TOKEN` and never prints passwords. The bootstrap token is for local administrative setup, not browser login.

## Roles

| Role | Access |
|---|---|
| admin | Agency clients, provisioning, users, policy publication/rollback, prices, decisions, audit and scoped kill controls |
| operator | Accessible client workflows, policy drafts/preview, budgets, credential versions, keys and client/workflow controls |
| approver | Accessible client data and action review/decisions |
| viewer | Read accessible client data |

A non-admin account can be limited to one `client_id`, or explicitly created for all clients by an administrator. Scope is enforced by the admin API on each read and mutation; browser visibility is not authorization. Only administrators may enable fail open. Approver identity is derived from the authenticated session.

The dashboard includes spend by client/workflow, budget reservations, one-time key creation and revocation, approval content review, alerts, provider job outcomes and comparison snapshots. Provider reporting failures mean unknown totals. Keys and decrypted provider credentials are never returned by list endpoints.

## Governance controls

The Governance page reuses budget, policy, and credential panels. Budget forms show client headroom, allow revision-checked cap changes, default new forms to explicit monthly recurrence, and expose run-stop reasons and reason-required clearing. A cap update and its future recurring schedule are separate operations; a schedule failure is displayed without undoing a successful cap change.

The guided policy builder provides workflow settings, flat named rules and condition rows, sample preview, revision history/comparison, and loading a prior revision. Operators save drafts and preview. Administrators additionally publish revisions and create rollbacks as new revisions. New operator-created workflows wait for administrator publication. Publication invalidates outstanding approvals and unconsumed tokens; callers must submit a new check. The separate RPM and loop request limits, notification channels, and outage mode are explicit settings. R2/R3 outage mode remains closed.

Credential controls create stable slots, stage replacements, activate with a version precondition, and retire versions. Admitted calls retain their selected version. Activation clears provider-account validation, and retiring the active version without replacement disables new calls for that slot. New key input clears after successful submission. See the [Phase 3 operating guide](phase3-controls.md) for API contracts, policy operators, key-ring maintenance, and upgrade requirements.

## Development and acceptance

In `dashboard/`, run `pnpm install --frozen-lockfile`, `pnpm typecheck`, `pnpm build`, and `pnpm start`. `ADMIN_API_URL` defaults to `http://localhost:8001`. Official references: [Next.js installation](https://nextjs.org/docs/app/getting-started/installation), [cookie API](https://nextjs.org/docs/app/api-reference/functions/cookies), [Playwright server setup](https://playwright.dev/docs/test-webserver).

`E2E_ADMIN_URL=http://localhost:8003 uv run python scripts/seed_e2e.py` (with the local Compose `maintenance` profile running) creates an isolated mock client, operator, administrator, and policy test workflow, then writes credentials with mode 0600 to `/private/tmp/gas-e2e.json` (`E2E_SEED_FILE` overrides). Then run `pnpm test` in `dashboard/`. Four tests cover a virtual key/native call/spend/stop flow, foreign-origin mutation rejection, operator draft/preview and administrator publication/rollback, and recurring client budgets with credential staging/activation/retirement. Mobile controls are checked at 390px. Reseed before rerunning: tests intentionally stop workflows and advance policy revisions. These generated accounts are only for the mock environment; remove the private seed file after acceptance.
