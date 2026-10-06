# Governed Automation Starter

Implementation of the design in `Claude.pdf`. The gateway proxies native provider dialects and applies budgets, action approval, audit logging, and reconciliation.

## Local development

1. Install Python 3.12 and `uv`; run `uv sync --frozen`.
2. Copy `.env.example` to `.env` and replace every placeholder secret.
3. Run `docker compose up --build` and apply Alembic migrations.
4. Run `uv run ruff check .`, `uv run mypy`, and `uv run pytest`.

The public gateway listens on port 8000, the separate administrative API on 8001, and the mock provider on 8002.
