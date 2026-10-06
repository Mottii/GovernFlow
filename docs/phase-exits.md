# Phase exit checks

## Phase 1 — 2026-10-06

The Postgres 16, gateway, admin API, and mock provider Compose services built and started. Alembic upgraded the container database to head. An isolated client, R1 workflow, 80 micro-USD monthly and run caps, mock-only OpenAI credential, and virtual key were created through the admin API. `GET /v1/models` returned `gpt-mock`. Repeated Chat calls through the gateway returned `200, 200, 200, 429`.

An n8n workflow with a Manual Trigger, Code node generating four items, and HTTP Request node calling the gateway was imported into the official n8n Docker image and executed with the same cap. The HTTP Request node received `429`; the workflow stopped with `NodeApiError: The service is receiving too many requests from you`. This satisfies the section 18 phase-one exit using the mock provider. The workflow was stored only in a temporary local n8n database because it contains a test virtual key.

For the exact OpenAI Base URL path, an `openAiApi` credential pointed at `http://gateway:8000/v1` and an n8n OpenAI Chat node processed twenty generated items. The first attempt exposed a budget/run row deadlock, which was fixed by using budget-then-run lock order in both reservation and settlement. The rerun ended on a gateway `429 budget_exceeded`, with no `500` or deadlock. Its final ledger showed 90 micro-USD spent, zero reserved, and zero pending reservations. The regression suite now mixes concurrent reservations and settlements.

The three reservations left by the failed pre-fix run were aged in the isolated mock database and released by `python -m policy.reap`. The ledger then showed zero reserved and zero pending, with 45 micro-USD of actual spend preserved.

## T2.1 — 2026-10-06

The official Anthropic Python SDK completed a non-streaming Messages call and a streamed Messages call against the gateway and mock provider. Both returned `mock reply` with 12 input and 3 output tokens. The adapter tests cover cache normalization, cumulative streamed usage, and an interrupted stream without final usage. The full suite passed with 14 tests; ruff and mypy passed.

## T2.2 — 2026-10-06

The official OpenAI Python SDK completed normal and streamed Responses and an embeddings request against the Compose gateway and mock provider. All 17 tests, ruff, and mypy passed.

## T2.3 — 2026-10-06

Plain HTTP tests completed Gemini generation, SSE streaming, and JSON-array streaming through the gateway. Requests and returned bytes matched the fixtures exactly, provider authentication was replaced, and all three calls settled their usage. The full suite passed with 19 tests; ruff and mypy passed. Contracts follow https://ai.google.dev/api/generate-content and https://ai.google.dev/api.

## T2.4 — 2026-10-06

The error matrix checks authentication, malformed requests, budget rejection, kill flags, upstream timeout, native upstream rejection, and storage outages on all five provider endpoints. Reservations returned to zero and error messages did not expose provider credentials or database details. All 21 tests, ruff, and mypy passed.

## T2.5 — 2026-10-06

All native routes rejected disallowed models with 403 and rate-limited calls with 429 and `Retry-After`. Fifty concurrent calls sharing a workflow admitted exactly seven at an RPM cap of seven. All 23 tests, ruff, and mypy passed, including migration upgrade/downgrade and the 200-call budget race.

## T2.6 — 2026-10-06

Fifteen-call soak tests stopped at configured step, run-cost, and loop-RPM thresholds, then returned kill responses. Alerts and audit chains verified. A provider charge of 1,000 micro-USD above its reservation was recorded in full and the next call was blocked. All 27 tests, ruff, and mypy passed.

## Phase 2 / T2.7 — 2026-10-06

The recorded mock contract matrix passes official OpenAI and Anthropic SDKs for plain and streamed endpoints, native tool bodies, long output, Gemini HTTP, and per-tenant upstream credentials. A simulated socket interruption settled an explicitly estimated charge and released its reservation. Fail-open eligibility, expiry, durable accounting, and single replay passed; all other workflows failed closed. All 37 tests passed against the running Docker Postgres 16, including the 200-call budget race and migration round trip. Ruff, mypy, and Compose configuration checks passed. Live provider captures remain pending credentials.

## T3.1 — 2026-10-06

Checks exercised all four risk classes, strict unknown-action denial, human approval and poll, unauthorized decision rejection, repeated decision rejection, and foreign-tenant approval IDs. All 38 tests passed against Postgres 16; ruff and mypy passed.

## T3.2 — 2026-10-06

The mocked Telegram Bot API received the summary, action content, and signed approve/deny callbacks. Review HTML escaped script content. Unauthorized callbacks were rejected, an authorized callback approved the action, and replay returned 409. All 39 tests passed against Postgres 16; ruff, mypy, and Compose checks passed. Live Telegram delivery is pending credentials; see `docs/live-setup.md`.

## T3.3 — 2026-10-06

SMTP and webhook mocks received full action content and review links. Incoming webhook decisions accepted a valid signature and rejected missing signatures, altered bodies, stale timestamps, and reused event IDs. All 40 tests passed against Postgres 16; ruff, mypy, and Compose checks passed. SMTP and external webhook delivery remain untested live.

## T3.4 — 2026-10-06

Twenty concurrent consumptions admitted exactly one execution. Altered actions, foreign workflows, and changed signatures were rejected. Python and JavaScript wrappers blocked pending actions and consumed approval before execution. All 41 backend tests passed against Postgres 16; the Node test, ruff, and backend/SDK mypy passed. The PDF's local HMAC verification instruction is corrected in `docs/decisions.md`.

## T3.5 — 2026-10-06

Pending approvals expired exactly once, following deny or explicitly configured low-risk allow outcomes. High-risk timeout allow was rejected. All 45 tests passed against Postgres 16; ruff, mypy, and Compose checks passed.

## Phase 3 / T3.6 — 2026-10-06

The action lifecycle test verified proposed, approved, denied, and expired events in order, approver metadata, encrypted content, tenant chains, and detection of altered decision metadata. Earlier checks proved pending actions cannot execute and consumption is single use. All 46 tests passed against Postgres 16; ruff and mypy passed. Telegram, SMTP, and external webhook live delivery remain pending credentials; mock contracts passed.
