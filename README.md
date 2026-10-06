# Governed Automation Starter

Section 18 of `Claude.pdf`, phases 1–4: a native provider gateway with budget reservations, approval tokens, append-only audit chains, reconciliation, and a role-based Next.js dashboard. The phase-5 live client pilot is outside this delivery.

## Start locally

Requires Docker with a running daemon, Python 3.12, and `uv`.

1. Run `uv sync --frozen`.
2. Copy `.env.example` to `.env`. Set `POSTGRES_PASSWORD`, a random `ADMIN_TOKEN`, and base64-encoded 32-byte random `MASTER_ENCRYPTION_KEY` and `DECISION_SIGNING_KEY`.
3. Run `docker compose build`, then `docker compose up -d postgres`.
4. Run `docker compose run --rm gateway alembic upgrade head` and `docker compose run --rm gateway python -m core.pricing`.
5. Run `docker compose up -d`.
6. Set `DASHBOARD_ADMIN_EMAIL` and a password of at least 12 characters in `.env`, then run `uv run python scripts/bootstrap_admin.py`.
7. Open [the dashboard](http://localhost:3000). The current development workspace already has an administrator; its email and password are in the ignored `.env`.

| Service | Local URL |
|---|---|
| Dashboard | http://localhost:3000 |
| Native gateway | http://localhost:8000 |
| Separate admin API | http://localhost:8001 |
| Mock provider | http://localhost:8002 |
| Postgres 16 | localhost:55433 |

Compose defaults to the mock provider. For live use, set `OPENAI_BASE_URL=https://api.openai.com`, `ANTHROPIC_BASE_URL=https://api.anthropic.com`, and `GEMINI_BASE_URL=https://generativelanguage.googleapis.com`. Configure encrypted inference credentials and validated dedicated OpenAI project / Anthropic workspace mappings before forwarding. See [live credentials](docs/live-setup.md), [roles and dashboard](docs/dashboard.md), [price scope](docs/prices.md), [daily reconciliation](docs/reconciliation.md), and [monthly reports](docs/reports.md).

## Workflow setup

Create a client, workflow with an explicit model allowlist, monthly budget, encrypted provider credential, and virtual key through the dashboard or `/admin/*` API using a scoped operator or administrator. Virtual keys are shown once. Use `Authorization: Bearer gk_…` for OpenAI, `x-api-key` for Anthropic, and `x-goog-api-key` or `?key=` for Gemini. For n8n OpenAI credentials, use `http://gateway:8000/v1` on the Compose network or `http://localhost:8000/v1` from the host.

Send a stable `X-Run-Id` per workflow execution to coordinate run limits. If omitted, calls share a ten-minute workflow window. New budgets are required for each UTC month. R2/R3 actions require approval through `/v1/actions/check`; the [Python and JavaScript wrappers](sdks/README.md) submit tokens to the gateway for atomic consumption before execution.

Native routes: `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, `/v1/messages`, `/v1beta/models/{model}:generateContent`, `/v1beta/models/{model}:streamGenerateContent`, and `/v1/models`. Admin routes are not served by the gateway.

Defaults fail closed. Explicit R0/R1 fail open is limited to known replica snapshots and durable estimated replay. Missing terminal stream usage is estimated. Final provider charges can exceed pre-call reservations; the full measured cost is recorded and subsequent calls stop. See [schema and policy decisions](docs/decisions.md).

## Verification

`uv run ruff check .`, `uv run mypy`, and `uv run pytest` (integration tests require `TEST_DATABASE_URL` and `APP_TEST_DATABASE_URL` pointing at separate disposable Postgres databases). Tests recreate their database tables. CI uses Postgres 16 and checks the 200-call race, tenant scope, SDK/native contracts, audit tampering, outages, and interrupted streams.

In `dashboard/`, use Node 22 and pnpm 11.19.0: `pnpm install --frozen-lockfile`, `pnpm typecheck`, `pnpm build`. Browser acceptance instructions are in [dashboard setup](docs/dashboard.md). JavaScript wrapper checks: `node --test sdks/javascript/index.test.mjs`.

[Acceptance evidence](docs/phase-exits.md) distinguishes passed mock checks from pending live checks. Telegram, SMTP, webhook delivery, and real provider reporting remain pending external credentials and reachable integration endpoints.
