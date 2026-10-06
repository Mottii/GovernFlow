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

## T4.1 — 2026-10-06

All six catalog models seeded idempotently with source URLs and UTC verification dates. Tests checked exact long-context and cache-TTL costs, admin authorization, invalid-rate rejection, and failure on unpriced hosted tools. All 47 tests passed against Postgres 16; ruff and mypy passed.

## T4.2 — 2026-10-06

Daily mock reporting compared OpenAI and Anthropic model usage and total costs with gateway events, including Anthropic cents conversion and filtering another workspace. The mapped OpenAI project header was verified; duplicate account assignments were rejected. A reporting outage recorded a failed job without replacing data with zeros. All 48 tests passed against Postgres 16; ruff, mypy, and Compose configuration checks passed. Live provider reporting remains pending organization credentials.

## T4.3 — 2026-10-06

A mock provider call absent from the gateway triggered `possible_bypass` in one reconciliation run. Repeated refreshes produced one alert; a matching refresh resolved it and the audit chain verified. All 49 tests passed against Postgres 16; ruff and mypy passed.

## T4.4 — 2026-10-06

All 50 backend tests passed against Postgres 16, including role restrictions and foreign-client IDs on every admin read and mutation. Ruff and mypy passed. TypeScript and the Next.js production build passed. Two Playwright tests passed against the Compose backend: create key, native call, visible spend, workflow pause and gateway 503 within the propagation limit, mobile controls and sign out, and rejection of foreign-origin mutations. Desktop and mobile screenshots were visually reviewed. Live provider calls remain pending credentials.

## Phase 4 / T4.5 — 2026-10-06

All 51 backend tests passed against Postgres 16. Monthly-report tests checked exact database/CSV/HTML cost totals, measured versus estimated savings, evidence updates, escaping, and tenant and role restrictions on every format. Ruff, backend/SDK mypy, TypeScript, and the Next.js production build passed. Two Playwright tests passed against the complete containerized stack, including saving savings evidence, downloading CSV, opening printable HTML, workflow pause, mobile sign out, and cross-origin rejection. The printable monthly report was visually reviewed. All Compose images built and services started; the database is at migration 0014 and the six-model catalog is seeded.

Phases 1–4 are implemented with one commit per section 18 task. Live Telegram, SMTP/webhook delivery, provider captures, and OpenAI/Anthropic reconciliation remain pending suitable credentials and external endpoints; deterministic mock and SDK contracts passed. The user does not currently have Telegram or provider reporting credentials. Acquisition steps are documented in `docs/live-setup.md`. Phase 5's live client pilot is outside this delivery.

## Publication review — 2026-10-06

The GovernFlow repository's existing MIT license commit was merged without rewriting the implementation history. Gitleaks 8.30.1 scanned Git history and the intended publication files with redacted output; no leaks were detected. A separate check compared local environment credential values and encoded variants against all reachable historical blobs and the PDF's extracted/OCR text; no matches were found. The PDF text and OCR output also passed Gitleaks. Temporary scan reports, local credentials, browser artifacts, and generated test login files remain outside tracked files.

Ignore rules now cover environment-file variants, common private-key files, and local secrets/credentials directories in Git and Docker build contexts. CI has a complete-history secret scan with a pinned, checksum-verified Gitleaks binary. Contributor and vulnerability-reporting guidance is included. Ruff, mypy, Compose configuration, workflow YAML parsing, and whitespace checks passed after these publication changes; application code is unchanged from the accepted phase-4 implementation.
