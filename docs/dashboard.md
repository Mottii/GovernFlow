# Dashboard

The Next.js dashboard serves port 3000 and calls the separate admin API through server-side route handlers. Login stores the session token in an HttpOnly, SameSite Strict cookie. Mutations require the dashboard's exact origin. Set `DASHBOARD_PUBLIC_URL` to your deployment's HTTPS origin; HTTPS enables Secure cookies. Session tokens are hashed in Postgres, expire after eight hours, and are revoked on sign out or account disable. Passwords use salted scrypt. Login attempts are limited in Postgres across replicas.

## First account

Set `DASHBOARD_ADMIN_EMAIL` and a password of at least 12 characters in the gitignored `.env`, then run `uv run python scripts/bootstrap_admin.py` after migrations and the admin service are ready. The script authenticates with the bootstrap `ADMIN_TOKEN` and never prints passwords. The bootstrap token is for local administrative setup, not browser login.

## Roles

| Role | Access |
|---|---|
| admin | All clients, provisioning, users, model prices, decisions and global/client/workflow kill controls |
| operator | Accessible client workflows, budgets, credentials, keys and client/workflow controls |
| approver | Accessible client data and action review/decisions |
| viewer | Read accessible client data |

A non-admin account can be limited to one `client_id`, or explicitly created for all clients by an administrator. Scope is enforced by the admin API on each read and mutation; browser visibility is not authorization. Only administrators may enable fail open. Approver identity is derived from the authenticated session.

The dashboard includes spend by client/workflow, budget reservations, one-time key creation and revocation, approval content review, alerts, provider job outcomes and comparison snapshots. Provider reporting failures mean unknown totals. Keys and decrypted provider credentials are never returned by list endpoints.

## Development and acceptance

In `dashboard/`, run `pnpm install --frozen-lockfile`, `pnpm typecheck`, `pnpm build`, and `pnpm start`. `ADMIN_API_URL` defaults to `http://localhost:8001`. Official references: [Next.js installation](https://nextjs.org/docs/app/getting-started/installation), [cookie API](https://nextjs.org/docs/app/api-reference/functions/cookies), [Playwright server setup](https://playwright.dev/docs/test-webserver).

`E2E_ADMIN_URL=http://localhost:8003 uv run python scripts/seed_e2e.py` (with the local Compose `maintenance` profile running) creates an isolated mock client and operator and writes credentials with mode 0600 to `/private/tmp/gas-e2e.json` (`E2E_SEED_FILE` overrides). Then run `pnpm test` in `dashboard/`. The test creates a virtual key, makes a native call, sees spend, pauses the workflow and observes the gateway block. It also checks mobile layout and rejection of foreign-origin mutations. These generated accounts are only for the mock environment.
