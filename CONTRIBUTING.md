# Contributing to GovernFlow

Everyone is welcome to suggest improvements, report bugs, improve documentation, or submit pull requests.

## Start with the mock environment

Follow the [README](README.md#start-locally) using Python 3.12, uv, Docker, Node 22, and pnpm 11.19.0. Compose uses deterministic mock providers by default; OpenAI, Anthropic, Gemini, and Telegram accounts are not required for the automated suite.

Create your own `.env` from `.env.example`. Generate local secrets for the database, admin setup, encryption, and signing. Never copy another person's credentials. Real inference keys are stored through the admin API, while reporting and notification credentials belong in local environment variables or a secret manager.

## Propose a change

1. Fork the repository and create a branch for your change.
2. Describe the problem and the intended behavior. For larger changes, discuss the approach in an issue first.
3. Keep changes focused and update the relevant documentation.
4. Run the applicable checks below and open a pull request. Include commands run, outcomes, and any live checks that remain unverified.

Maintainers review and merge contributions. Submitted contributions are made under the project's [MIT license](LICENSE).

## Backend checks

Run from the repository root:

```sh
uv sync --frozen
uv run ruff check .
uv run mypy
uv run mypy --strict sdks/python/governed_automation
uv run pytest
node --test sdks/javascript/index.test.mjs
APP_DATABASE_PASSWORD=compose_validation_only_secret docker compose config --quiet
```

Integration tests require `TEST_DATABASE_URL` and `APP_TEST_DATABASE_URL` pointing to **two separate disposable Postgres 16 databases**. The suite recreates tables and exercises migration downgrade/upgrade. Use databases dedicated to testing. `.github/workflows/ci.yml` shows the automated setup.

## Dashboard checks

In `dashboard/`:

```sh
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
```

Start the Compose backend (its initializer applies migrations), enable the local `maintenance` profile, then run `E2E_ADMIN_URL=http://localhost:8003 uv run python scripts/seed_e2e.py` from the repository root. This creates an isolated mock tenant and saves test login credentials in a private temporary file. Run `pnpm test` in `dashboard/` to check the browser flow. See [dashboard setup](docs/dashboard.md) for details.

## Governance changes

Preserve native request and response contracts, integer micro-USD accounting, UTC timestamps, tenant scope, single-use approval consumption, and audit transaction boundaries. Document schema changes in [docs/decisions.md](docs/decisions.md). Add regression coverage for changed behavior, particularly concurrency, outages, streaming, and authorization.

Live provider and notification checks are separate from mock checks and can incur charges. Use dedicated test projects/workspaces, short prompts, and explicit budgets. Report them as passed only after executing them with real services.

## Credentials and fixtures

Keep `.env` variants, private keys, database exports, notification payloads, browser traces, and generated login fixtures local. Use fictitious credentials and synthetic content in tests. Sanitize any recorded provider response before adding it as a fixture. The CI secret scan checks all reachable Git history; deleting a leaked key from the newest commit does not remove it from earlier commits.

If a real key is exposed, revoke or rotate it immediately and contact the maintainers privately. See [SECURITY.md](SECURITY.md).
