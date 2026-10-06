# Phase exit checks

## Phase 1 — 2026-10-06

The Postgres 16, gateway, admin API, and mock provider Compose services built and started. Alembic upgraded the container database to head. An isolated client, R1 workflow, 80 micro-USD monthly and run caps, mock-only OpenAI credential, and virtual key were created through the admin API. `GET /v1/models` returned `gpt-mock`. Repeated Chat calls through the gateway returned `200, 200, 200, 429`.

An n8n workflow with a Manual Trigger, Code node generating four items, and HTTP Request node calling the gateway was imported into the official n8n Docker image and executed with the same cap. The HTTP Request node received `429`; the workflow stopped with `NodeApiError: The service is receiving too many requests from you`. This satisfies the section 18 phase-one exit using the mock provider. The workflow was stored only in a temporary local n8n database because it contains a test virtual key.

For the exact OpenAI Base URL path, an `openAiApi` credential pointed at `http://gateway:8000/v1` and an n8n OpenAI Chat node processed twenty generated items. The first attempt exposed a budget/run row deadlock, which was fixed by using budget-then-run lock order in both reservation and settlement. The rerun ended on a gateway `429 budget_exceeded`, with no `500` or deadlock. Its final ledger showed 90 micro-USD spent, zero reserved, and zero pending reservations. The regression suite now mixes concurrent reservations and settlements.

The three reservations left by the failed pre-fix run were aged in the isolated mock database and released by `python -m policy.reap`. The ledger then showed zero reserved and zero pending, with 45 micro-USD of actual spend preserved.

## T2.1 — 2026-10-06

The official Anthropic Python SDK completed a non-streaming Messages call and a streamed Messages call against the gateway and mock provider. Both returned `mock reply` with 12 input and 3 output tokens. The adapter tests cover cache normalization, cumulative streamed usage, and an interrupted stream without final usage. The full suite passed with 14 tests; ruff and mypy passed.
