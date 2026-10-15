# GovernFlow

The agency release is in progress. See the [release brief](docs/agency-release.md) and [implementation status](docs/agency-release-status.md) for current acceptance evidence and remaining gates.

**Budgets, approvals, and audit for AI automation.**

[![CI](https://github.com/Mottii/GovernFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/Mottii/GovernFlow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

GovernFlow is an open-source gateway for teams running AI workflows. It sits between your automation and its model providers, enforces spending and access policies, asks people to approve sensitive actions, and records what happened. A Next.js dashboard brings the controls and reports together.

It supports native OpenAI, Anthropic, and Gemini request formats. You keep using the provider's SDK or HTTP API and point supported calls at GovernFlow with a workflow virtual key.

## Project status

The starter implements section 18, phases 1–4, of [the project specification](Claude.pdf). Each implementation task has its own commit. The phase-5 live client pilot is outside this delivery.

- **Verified with mocks:** Postgres-backed budget concurrency, native API and official SDK contracts, interrupted streams, tenant isolation, approvals and single-use consumption, audit tampering detection, reconciliation, reports, and dashboard browser flows.
- **Pending live verification:** Real provider calls and captures, OpenAI/Anthropic organization reporting, Telegram delivery, SMTP delivery, and external webhook delivery. These require suitable accounts, credentials, and reachable endpoints.
- **Local demo:** Docker Compose defaults to a mock provider. No paid inference account is needed for this demo.

See [acceptance evidence](docs/phase-exits.md) for the checks performed and [implementation decisions](docs/decisions.md) for corrections and schema departures from the specification. Treat this as a starter to validate in your environment before a client deployment.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Start locally](#start-locally)
- [Try the mock demo](#try-the-mock-demo)
- [API routes](#api-routes)
- [Connect an automation](#connect-an-automation)
- [Workflow policies and budgets](#workflow-policies-and-budgets)
- [Action approvals](#action-approvals)
- [Dashboard and roles](#dashboard-and-roles)
- [Connect live providers](#connect-live-providers)
- [Reconciliation and reports](#reconciliation-and-reports)
- [Configuration](#configuration)
- [Operations and deployment](#operations-and-deployment)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Repository guide](#repository-guide)
- [Contributing](#contributing)
- [License](#license)

## Features

| Capability | What GovernFlow provides |
| --- | --- |
| Native model APIs | OpenAI Chat Completions, Responses, and embeddings; Anthropic Messages; Gemini generation and streaming |
| Tenant isolation | Client-scoped workflows, credentials, virtual keys, approvals, audit events, and reports |
| Credential protection | AES-GCM encryption for provider credentials and hashes for workflow virtual keys |
| Spending controls | Atomic monthly and per-run reservations in Postgres; integer micro-USD accounting |
| Streaming accounting | Usage parsed from a copy of the stream; interrupted or unmeasured calls explicitly estimated |
| Workflow controls | Model allowlists, shared rate limits, step limits, loop breaking, and kill flags |
| Action approvals | Check, poll, human decision, expiry, and atomic single-use token consumption |
| Approval channels | Dashboard, Telegram, SMTP review links, and signed webhooks |
| Audit | Append-only hash chains per client, with a verification command |
| Provider reconciliation | Daily OpenAI project and Anthropic workspace comparisons with drift alerts |
| Reporting | Monthly JSON, CSV, and printable HTML; documented measured or estimated savings |
| Dashboard | Separate admin API, authenticated sessions, and four roles with optional client scope |
| Verification | Postgres integration tests, SDK fixtures, browser acceptance, and Git-history credential scanning in CI |

## How it works

```mermaid
flowchart LR
    W[Automation / n8n / SDK] -->|Workflow virtual key| G[Gateway :8000]
    G --> O[OpenAI]
    G --> A[Anthropic]
    G --> M[Gemini]
    G <--> P[(Postgres 16)]
    D[Dashboard :3000] --> B[Admin API :8001]
    B <--> P
    H[Telegram / email / webhooks] --> B
    J[Maintenance and reporting workers] <--> P
```

For a model call, the gateway:

1. Authenticates the virtual key and identifies its client and workflow.
2. Checks kill flags, the model allowlist, pricing, rate limits, and run limits.
3. Atomically reserves an estimated charge against the monthly and run budgets.
4. Forwards to the selected provider using the client's encrypted inference credential.
5. Reads usage, settles the reservation, and appends an audit event.
6. Records the full cost and stops subsequent calls if an overrun trips the workflow.

The gateway preserves each provider's dialect and native error shape. It does not translate an Anthropic request into OpenAI or switch providers automatically. Streaming response bytes are forwarded while a copy is parsed for accounting. For OpenAI Chat, the gateway may request the final usage chunk and hide a usage-only chunk it injected when the caller did not request one.

Action governance is a separate flow. Your automation must check an action and consume its approval before performing the external operation. The supplied wrappers do this for you. Provider calls or business actions that bypass these integrations cannot be stopped by the gateway; supported provider reporting can help identify unexplained usage.

## Start locally

### Requirements

- Python **3.12** and [`uv`](https://docs.astral.sh/uv/).
- Docker with the Compose plugin and a running daemon.
- Git.
- For dashboard development outside Docker: Node **22** and pnpm **11.19.0**.

The backend uses FastAPI, SQLAlchemy, Alembic, and Postgres 16. The dashboard uses Next.js 16 and React 19. Docker supplies the application runtimes for the containerized setup.

### 1. Clone and install

```bash
git clone https://github.com/Mottii/GovernFlow.git
cd GovernFlow
uv sync --frozen
```

### 2. Create a private local environment

For a fresh checkout, this generates local secrets and a dashboard login in `.env`, with file permissions `0600`. It refuses to overwrite an existing file and prints no secret values.

```bash
uv run python - <<'PY'
import base64
import os
import secrets
from pathlib import Path

password = secrets.token_hex(24)
app_password = secrets.token_urlsafe(32)
values = {
    "POSTGRES_PASSWORD": password,
    "APP_DATABASE_PASSWORD": app_password,
    "DATABASE_URL": f"postgresql+asyncpg://governflow_app:{app_password}@localhost:55433/gateway",
    "ADMIN_TOKEN": secrets.token_urlsafe(32),
    "MASTER_ENCRYPTION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    "DECISION_SIGNING_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    "DASHBOARD_ADMIN_EMAIL": "admin@local.test",
    "DASHBOARD_ADMIN_PASSWORD": secrets.token_urlsafe(24),
}
lines = []
for line in Path(".env.example").read_text().splitlines():
    name = line.partition("=")[0]
    lines.append(f"{name}={values[name]}" if name in values else line)
fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write("\n".join(lines) + "\n")
print("Created private .env. Read its dashboard login locally.")
PY
```

If `.env` already exists, retain its keys and configure missing settings locally. Changing `MASTER_ENCRYPTION_KEY` without re-encrypting stored credentials makes those credentials unreadable. Keep `.env` out of Git, screenshots, issue attachments, and logs.

### 3. Build, migrate, and start

```bash
docker compose build
docker compose up -d
uv run python scripts/bootstrap_admin.py
```

Open [the dashboard](http://localhost:3000) and sign in with `DASHBOARD_ADMIN_EMAIL` and `DASHBOARD_ADMIN_PASSWORD` from your local `.env`. `ADMIN_TOKEN` is a bootstrap API credential, not the dashboard password.

| Service | Local address | Purpose |
| --- | --- | --- |
| Dashboard | <http://localhost:3000> | Login, governance controls, reports |
| Gateway | <http://localhost:8000> | Native provider APIs and action checks |
| Admin API | <http://localhost:8001> | Provisioning, decisions, users, reporting |
| Mock provider | <http://localhost:8002> | Deterministic development responses |
| Postgres | `localhost:55433` | Persistent governance data |

The `initialize` service migrates and seeds as the database owner, then provisions `governflow_app` with the restricted `governflow_runtime` grants. Gateway, admin API, and every worker use that application login. Runtime connections refuse owner, superuser, or bypass-RLS identities. Supply `APP_DATABASE_PASSWORD` when upgrading an existing `.env`; keep the existing owner and encryption secrets.

Platform agency provisioning and catalog changes use the optional `maintenance` profile, bound to `127.0.0.1:8003`. Start it only for operator work (`docker compose --profile maintenance up -d maintenance`) and stop it afterward. `MAINTENANCE_MODE=true` is confined to initialization and that operator service. Ordinary admin API bootstrap access cannot mutate the shared catalog. See [agency administration](docs/agency-administration.md) for reviewer and destination setup.

`docker compose down` stops the stack while retaining named volumes. Adding `--volumes` deletes the local database and replay queue. Migrations and catalog seeding must run before using a fresh database.

## Try the mock demo

### 1. Create a demo client

With the stack running, execute from the repository root:

```bash
docker compose --profile maintenance up -d maintenance
E2E_ADMIN_URL=http://localhost:8003 E2E_SEED_FILE=.e2e.json uv run python scripts/seed_e2e.py
docker compose stop maintenance
```

This creates an isolated mock client, an R1 workflow, an operator account, a mock inference credential, and a `gpt-mock` price. The workflow has a **$1 monthly budget** and a **$0.01 per-run cap**. These are simulated ledger amounts; the mock provider incurs no inference charge.

The generated login and IDs are stored in the ignored `.e2e.json` file with permissions `0600`. Use that operator login, or your administrator login, to select the new workflow. In **Workflows**, create a virtual key and save it privately; the full key is shown only once.

### 2. Call the gateway

Load the virtual key without putting its literal value in shell history:

```bash
printf 'Workflow virtual key: '
read -r -s GOVERNFLOW_VIRTUAL_KEY
printf '\n'
export GOVERNFLOW_VIRTUAL_KEY
```

List allowed, priced models:

```bash
curl --fail-with-body http://localhost:8000/v1/models \
  -H "Authorization: Bearer $GOVERNFLOW_VIRTUAL_KEY"
```

Make a native Chat Completions call:

```bash
curl --fail-with-body http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $GOVERNFLOW_VIRTUAL_KEY" \
  -H 'Content-Type: application/json' \
  -H 'X-Run-Id: demo-run-001' \
  -d '{"model":"gpt-mock","max_tokens":16,"messages":[{"role":"user","content":"Hello"}]}'
```

The mock returns `mock reply` and usage. Refresh the dashboard to see spend and reservations. Pause the workflow and repeat the call: it should be blocked after kill-flag propagation, normally within five seconds. Resume it to continue.

The demo seeds the OpenAI path. Testing Anthropic or Gemini also requires a matching provider credential, model price, and workflow allowlist.

## API routes

The gateway and admin API are separate FastAPI applications. Native calls and action checks are served on port 8000; `/admin/*` is served on port 8001. Each service exposes interactive API documentation at `/docs`.

| Method | Gateway route | Authentication |
| --- | --- | --- |
| GET | `/v1/models` | `Authorization: Bearer <virtual-key>` |
| POST | `/v1/chat/completions` | Bearer virtual key |
| POST | `/v1/responses` | Bearer virtual key |
| POST | `/v1/embeddings` | Bearer virtual key |
| POST | `/v1/messages` | `x-api-key: <virtual-key>` |
| POST | `/v1beta/models/{model}:generateContent` | `x-goog-api-key: <virtual-key>` or `?key=` |
| POST | `/v1beta/models/{model}:streamGenerateContent` | Gemini virtual key; supports SSE and JSON-array streaming |
| POST | `/v1/actions/check` | Bearer virtual key |
| GET | `/v1/actions/{approval_id}` | Bearer virtual key |
| POST | `/v1/actions/consume` | Bearer virtual key |

Gemini generation routes also accept the `/v1/models/{model}:...` path. Prefer the authentication header over a query key when possible, since URLs often appear in access logs. Preserve provider-specific headers such as Anthropic's API version.

Important admin resources include clients, workflows, budgets, provider credentials, virtual keys, users, prices, provider accounts, approvals, kill controls, alerts, reconciliation, and reports. Human decisions use `POST /admin/actions/{approval_id}/decision` with an authorized administrator or approver. Use the dashboard or the admin API's OpenAPI schema for exact request shapes. Admin authentication accepts dashboard sessions or the bootstrap administrator token.

## Connect an automation

### OpenAI Python SDK

The development dependencies include the official OpenAI SDK. From the repository environment:

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["GOVERNFLOW_VIRTUAL_KEY"],
    base_url="http://localhost:8000/v1",
    max_retries=0,
)
response = client.chat.completions.create(
    model="gpt-mock",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=16,
    extra_headers={"X-Run-Id": "sdk-demo-001"},
)
print(response.choices[0].message.content)
```

For streaming, add `stream=True` and iterate over chunks. Responses and embeddings use the same OpenAI base URL, with their respective methods and allowed, priced models. Choose retry behavior deliberately: retries are additional calls and may incur additional spend.

### Anthropic and Gemini

For the Anthropic SDK, set its base URL to `http://localhost:8000` and its API key to the workflow virtual key. Send the native Messages body and version header. Configure the workflow's Anthropic inference credential and model price first.

For Gemini, send the native `contents` body to the generation routes with `x-goog-api-key` set to the virtual key. For SSE, use `streamGenerateContent?alt=sse`. No cross-provider request conversion is required.

### n8n

Create OpenAI credentials in n8n using a workflow virtual key and the gateway base URL:

- n8n running on the host: `http://localhost:8000/v1`.
- n8n on the same Docker Compose network: `http://gateway:8000/v1`.
- n8n in another container/network: configure a reachable host address or join the gateway's network.

Where the node supports custom headers, use the n8n execution ID as `X-Run-Id`. The acceptance evidence includes an actual n8n workflow stopped by a gateway budget against the mock provider.

## Workflow policies and budgets

A workflow belongs to a client and has a risk class and JSON policy. For example, the `policy` field in `POST /admin/workflows` can contain:

```json
{
  "models": ["gpt-4.1-mini"],
  "rpm": 60,
  "per_run_micro": 500000,
  "loop_breaker": {
    "max_steps": 25,
    "rpm": 120,
    "run_cost_micro": 500000
  },
  "strict_actions": true,
  "actions": {
    "send_email": {
      "risk_class": "R2",
      "timeout_seconds": 3600,
      "on_timeout": "deny"
    }
  },
  "outage": {"mode": "closed"}
}
```

This live-model example requires a configured provider credential and model price. The mock demo uses `gpt-mock` instead.

### Budget rules

- **Money:** `1 USD = 1,000,000 micro-USD`. Budget and cost fields are integers; `500000` represents $0.50.
- **Monthly cap:** Create a budget for the workflow and the current UTC `YYYY-MM`. Provision the next month's budget explicitly; there is no automatic rollover.
- **Per-run cap:** `per_run_micro`, nested `budget.per_run_micro`, and `loop_breaker.run_cost_micro` are supported. If several are supplied, the smallest applies.
- **Run identity:** Send a stable `X-Run-Id` for each execution. Reusing it reuses that run's accounting. Without it, calls share a ten-minute workflow window.
- **Access:** Models must match the allowlist and have a usable price. An absent allowlist denies model access.
- **Concurrency:** Postgres locks coordinate reservations across replicas so concurrent calls cannot reserve the same remaining balance.
- **Overruns:** If actual cost exceeds a reservation, the full charge is recorded, `budget_overrun` is emitted, and subsequent calls stop. A pre-call estimate cannot guarantee that final provider charges never exceed a cap.

Set native output-token limits explicitly. Missing terminal stream usage, including an interrupted OpenAI stream, settles an explicitly marked estimate. Estimates remain visible in audit events and reports.

### Outage behavior

Workflows fail closed by default. Only an administrator may enable `outage.mode = "open"`, and only for R0/R1 workflows.

A replica can admit such a call during a storage outage only if it has a recent eligible snapshot from a successful governed call. Snapshots expire after 60 seconds. The call is written to a durable replay queue before forwarding, receives `x-gateway-estimated: true`, and is replayed into accounting when storage returns. Unknown workflows, expired snapshots, R2/R3 workflows, or an unavailable replay queue fail closed.

During a database outage, replicas cannot coordinate a global remaining budget or receive fresh kill state. Enable this exception only when that limitation is acceptable, and persist the replay volume. See [the outage and schema decisions](docs/decisions.md).

## Action approvals

The gateway checks proposed business actions separately from model calls. Configure action names in the workflow policy; the default is to deny unknown actions.

| Effective risk | Default action behavior |
| --- | --- |
| R0 / R1 | Allow a configured action automatically unless `require_approval` is enabled |
| R2 / R3 | Wait for a human decision |
| Unknown action | Deny under `strict_actions: true` |
| Expired request | Deny; explicit low-risk timeout allowance is supported |

Effective risk is the higher of the workflow and action risk. R2/R3 timeout allowance is rejected.

The lifecycle is **check → pending/review → decision → token → consume → execute**. The gateway verifies the token's HMAC, workflow, action hash, status, and expiry, then consumes it atomically. Clients never receive the signing secret. Tokens last at most 15 minutes after approval, subject to the approval deadline. Concurrent consumption permits only one successful attempt.

Consumption happens before your callback. If the callback fails, obtain another approval before retrying. This protects one execution attempt; it does not make an external email, payment, or database operation exactly once.

### Python wrapper

Install from the repository root:

```bash
uv pip install ./sdks/python
```

Create a workflow with the `send_email` action rule through `POST /admin/workflows`, then obtain its virtual key before running this example. The starter dashboard's workflow form exposes basic model and budget settings; use the API for the full policy object. The mock seed's workflow has no action rules. This callback only prints a demo message; replace it with your business operation after testing.

```python
import asyncio
import os
from governed_automation import Gateway

async def main():
    gateway = Gateway("http://localhost:8000", os.environ["GOVERNFLOW_VIRTUAL_KEY"])
    action = {"recipient": "review@example.test", "body": "Demo message"}

    async def send_demo():
        print("Approved demo callback executed")

    try:
        await gateway.execute(
            "send_email", action, "Review demo email", send_demo, wait_timeout=900
        )
    finally:
        await gateway.close()

asyncio.run(main())
```

### JavaScript wrapper

From a Node 22+ application running at the repository root:

```javascript
import {Gateway} from './sdks/javascript/index.mjs';

const gateway = new Gateway(
  'http://localhost:8000', process.env.GOVERNFLOW_VIRTUAL_KEY
);
await gateway.execute(
  'send_email',
  {recipient: 'review@example.test', body: 'Demo message'},
  'Review demo email',
  async () => console.log('Approved demo callback executed'),
  {waitTimeout: 900000}
);
```

Python wait time is in seconds; JavaScript wait time is in milliseconds. Both wrappers expose `check_action` and `verify_token`; despite its name, `verify_token` **consumes** the approval. See [SDK details](sdks/README.md).

### Human review channels

People can decide in the dashboard or through configured Telegram, SMTP review links, or signed webhooks. Notification delivery and expiry run through the `approval-notifier` worker. Telegram callbacks restrict approvers; webhook decisions validate signatures, timestamps, and replay identifiers. Approval content and decisions enter the audit chain.

Follow [live credential setup](docs/live-setup.md) for Telegram. Configure transport settings below and explicitly assign reviewer groups and destinations through [agency administration](docs/agency-administration.md). These external delivery paths have mock acceptance coverage and still require live verification.

## Dashboard and roles

The dashboard includes **Overview, Workflows, Approvals, Alerts, Reconciliation, Reports, and Settings**. It calls the separate admin API through server-side handlers.

| Role | Access |
| --- | --- |
| Administrator | All clients; provisioning, users, prices, decisions, and global/client/workflow kill controls |
| Operator | Accessible client workflows, budgets, credentials, virtual keys, reports, and client/workflow controls |
| Approver | Accessible client data and approval decisions |
| Viewer | Read accessible client data |

Non-admin accounts may be scoped to one client, or explicitly granted access to all clients by an administrator. The API enforces scope and roles on each operation.

Sessions use HttpOnly, SameSite Strict cookies, hashed tokens stored in Postgres, and an eight-hour expiry. Passwords use salted scrypt. Login rate limits are shared through Postgres. Mutations require the configured dashboard origin. Set `DASHBOARD_PUBLIC_URL` to an HTTPS origin in deployment to enable Secure cookies.

Virtual keys are shown once on creation; list endpoints return identifying metadata. Decrypted provider credentials are not returned. See [dashboard details](docs/dashboard.md).

## Connect live providers

### 1. Configure upstream origins

Add these settings to your ignored `.env` to replace Compose's mock defaults:

```dotenv
OPENAI_BASE_URL=https://api.openai.com
ANTHROPIC_BASE_URL=https://api.anthropic.com
GEMINI_BASE_URL=https://generativelanguage.googleapis.com
```

After changing environment settings, recreate affected containers with `docker compose up -d`.

### 2. Create credentials and account mappings

For each client:

1. Create the required inference keys in the provider accounts.
2. Store inference keys through the dashboard or `POST /admin/provider-credentials`; they are encrypted in Postgres.
3. For OpenAI, dedicate a project to the client. For Anthropic, dedicate a workspace to the client.
4. Set organization reporting credentials in `OPENAI_ADMIN_KEY` and `ANTHROPIC_ADMIN_KEY`.
5. Create `/admin/provider-accounts` mappings using the client ID, provider, external project/workspace ID, and credential label. Anthropic also requires the inference key's metadata ID.
6. Validate mappings live, configure prices and the allowlist, and create the UTC monthly budget and workflow virtual key.

`OPENAI_PROJECT_ID` and `ANTHROPIC_WORKSPACE_ID` in the template are setup references; setting them alone does not create database mappings. `validate_live: false` supports mock fixtures. Forwarding to official OpenAI/Anthropic hosts requires validated mappings even when the custom-origin mapping option is disabled.

Organization reporting credentials are separate from inference credentials. See [how to acquire them](docs/live-setup.md) and [mapping and reconciliation details](docs/reconciliation.md).

### 3. Confirm metering before widening access

The effective-dated catalog records source URLs and verification times. See [price scope](docs/prices.md) and [the catalog](core/model_prices.json). Reverify provider formats and prices before live acceptance and model changes.

The starter meters supported standard token billing. Unpriced hosted tools, alternate service tiers, audio/video, generated media, and background Responses are rejected until their billing is supported. Native function tools are supported. Gemini cache-storage charges are outside these proxy routes; Gemini remains gateway-observed in v1.

Begin with short prompts and explicit small output limits. Real inference calls and infrastructure can cost money; running the local mock demo does not require buying provider credits. Live reporting and notification checks remain pending rather than being implied by mock success.

## Reconciliation and reports

### Daily provider comparisons

The reconciler refreshes the preceding three UTC days every 24 hours to accommodate delayed reporting. Run it manually in the configured container:

```bash
docker compose exec reconciler python -m reconcile.service
```

Add `--day YYYY-MM-DD` to refresh a particular day.

| Provider | Usage comparison | Cost comparison |
| --- | --- | --- |
| OpenAI | Model-level usage within the client's dedicated project | Project and line-item grouping; the cost API does not expose model-level costs |
| Anthropic | Model-level usage within the client's dedicated workspace | Workspace and description grouping; provider cents converted to micro-USD |
| Gemini | Gateway-observed only | Gateway-observed only |

Unexplained positive drift is labeled **possible bypass**. Estimated calls, negative differences, reporting delays, UTC boundaries, and unsupported billing categories can require investigation. A drift alert is not proof of bypass. Alerts are deduplicated and resolve when later successful comparisons match.

Failed reporting jobs preserve the last successful snapshot and show the failure; missing reporting is unknown, not zero spend. See [reconciliation details](docs/reconciliation.md).

### Monthly exports

Use the dashboard's Reports section, or the admin API:

- `GET /admin/reports/{client_id}/{YYYY-MM}` — JSON.
- Add `?format=csv` — CSV download with exact micro-USD fields.
- Add `?format=html` — printable HTML; use the browser's Print command.

Report costs sum provider-call audit events settled during the UTC month. Budget spend and reservations are listed separately, because a reservation can settle in a later month. Estimated calls are identified.

Administrators and operators can supply minutes saved with a measured or estimated basis and an evidence note. Savings updates are audited; call counts do not create automatic savings claims. Exports enforce tenant scope and escape HTML or spreadsheet formula content. See [report details](docs/reports.md).

## Configuration

Start with [.env.example](.env.example). Keep real values in the ignored `.env` or a secret manager. Compose explicitly passes settings to each service; when adding a process-level setting, also wire it into that service's environment.

| Setting | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | Local Postgres password; also used by Compose's internal database URLs |
| `DATABASE_URL` | Restricted application login for host-side tools; Compose uses its internal `postgres` hostname |
| `APP_DATABASE_PASSWORD` | URL-safe generated runtime password, at least 20 characters; separate from the migration owner |
| `POSTGRES_HOST_PORT` | Optional Compose host port; defaults to `55433` |
| `MASTER_ENCRYPTION_KEY` | Base64-encoded 32-byte key for encrypted credentials and approval content |
| `DECISION_SIGNING_KEY` | Base64-encoded 32-byte action token signing key; gateway-side only for SDK use |
| `ADMIN_TOKEN` | Bootstrap administrator bearer token |
| `DASHBOARD_ADMIN_EMAIL`, `DASHBOARD_ADMIN_PASSWORD` | Initial administrator created by the bootstrap script; password must be at least 12 characters |
| `ADMIN_API_URL` | Dashboard server's admin API origin; Compose sets `http://admin-api:8001` |
| `DASHBOARD_PUBLIC_URL` | Exact browser origin; use HTTPS for deployment |
| `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, `GEMINI_BASE_URL` | Provider origins; Compose defaults to the mock provider |
| `UPSTREAM_TIMEOUT_S` | Provider HTTP timeout; default `120` seconds |
| `REQUIRE_PROVIDER_MAPPING` | Mapping requirement for custom origins; official OpenAI/Anthropic hosts always require validated mappings |
| `OPENAI_ADMIN_KEY`, `ANTHROPIC_ADMIN_KEY` | Organization reporting and account-validation credentials |
| `DRIFT_COST_TOLERANCE_MICRO`, `DRIFT_TOKEN_TOLERANCE` | Drift tolerances; defaults are one micro-USD and zero tokens |
| `TELEGRAM_BOT_TOKEN` | Telegram delivery transport; recipients require user account linking and a scoped destination |
| `TELEGRAM_WEBHOOK_SECRET`, `PUBLIC_ADMIN_URL` | Telegram callback authentication and reachable admin origin; live webhook setup needs HTTPS |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM` | Email delivery endpoint and sender; default port `587` |
| `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_TLS_MODE` | SMTP authentication and TLS policy; default mode `starttls` |
| `TELEGRAM_APPROVER_CHAT_IDS`, `APPROVAL_EMAIL_TO`, `APPROVAL_WEBHOOK_URL` | Legacy values imported as encrypted, disabled destinations during upgrade; no routing authority |
| Webhook destination credentials | Generated per destination by the admin API; the former global signing key is no longer used |
| `REPLAY_QUEUE_PATH` | Durable SQLite replay queue path; Compose mounts the named `replay_queue` volume |
| `E2E_SEED_FILE` | Private generated mock acceptance login file; use `.e2e.json` from the repository root |

`DEFAULT_FAIL_MODE` in the template does not override workflow policy: fail open requires an explicit administrator-approved policy. The Chat handler reads `DEFAULT_MAX_OUTPUT_TOKENS` as a reservation-estimate fallback, defaulting to 4096; Compose does not currently pass that variable through. Send the provider's native output-limit field explicitly. An estimate is not an injected provider output cap.

## Operations and deployment

### Useful commands

```bash
docker compose ps
docker compose logs --tail=100 gateway admin-api
docker compose run --rm initialize alembic current
docker compose run --rm initialize python -m core.pricing
```

Verify a client's audit chain after setting its non-secret ID:

```bash
export GOVERNFLOW_CLIENT_ID='replace-with-client-uuid'
docker compose exec gateway python -m audit.verify --client "$GOVERNFLOW_CLIENT_ID"
```

| Worker | Schedule in Compose | Responsibility |
| --- | --- | --- |
| `reservation-reaper` | Every 60 seconds | Release stale reservations, replay queued accounting, clean expired sessions/rate buckets |
| `approval-notifier` | Every 30 seconds | Expire approvals and deliver queued notifications |
| `reconciler` | Every 24 hours | Refresh three prior UTC days and detect drift |

Stale pending reservations older than one hour are released by maintenance. Keep workers running and monitor their errors. Use the global, client, and workflow kill controls when intervention is needed.

### Deploying beyond localhost

The supplied Compose file is for local development. For a hosted deployment:

1. Put the dashboard and required API paths behind HTTPS and configure their public origins.
2. Restrict direct access to Postgres and the admin API; expose only the admin paths needed for authorized users and notification callbacks.
3. Supply stable encryption/signing keys and credentials through a secret manager.
4. Persist and back up Postgres and the replay queue; test restoration with the original encryption key.
5. Run migrations, verify prices, validate client account mappings, and replace mock upstreams.
6. Monitor workers, budget overruns, kill state, notification failures, and reconciliation failures.
7. Complete the live acceptance checks before routing client workflows.

### Current boundaries

- Governance depends on routing calls and integrating action checks. A direct provider request bypasses gateway enforcement.
- Estimated reservations cannot promise an absolute final provider charge cap.
- Fail-open replicas cannot coordinate budgets or fresh kill flags during storage outages.
- Audit chains detect changes against a trusted chain state; they are not externally notarized and do not prevent a database administrator from rewriting all history.
- Gemini has no provider billing reconciliation in v1.
- New UTC monthly budgets must be provisioned explicitly.
- Client `retention_days` is stored metadata; automatic deletion of client history is not implemented.
- Single-use approval consumption protects one attempt, not exactly-once execution of external operations.
- Live provider and notification acceptance is still outstanding. The live client pilot is outside phases 1–4.

## Testing

### Backend and wrappers

```bash
uv run ruff check .
uv run mypy
uv run mypy --strict sdks/python/governed_automation
uv run pytest
node --test sdks/javascript/index.test.mjs
docker compose config --quiet
```

For full integration coverage, set **both** `TEST_DATABASE_URL` and `APP_TEST_DATABASE_URL` to separate, disposable Postgres 16 databases. The migration test upgrades and downgrades its database; application fixtures recreate tables in theirs. Never point these variables at your development or production data. When they are absent, database tests are skipped, so a partial local pass is not full acceptance.

Coverage includes the 200-call budget race, mixed reservation/settlement concurrency, per-run caps, tenant isolation, native error shapes, official OpenAI/Anthropic SDK contracts, stream interruption, outages, approval replay, expiry, audit-chain tampering, provider drift, and report exports. Fixtures are deterministic mocks following provider formats, not live captures.

### Dashboard

With the mock services running, from the repository root:

```bash
docker compose --profile maintenance up -d maintenance
E2E_ADMIN_URL=http://localhost:8003 E2E_SEED_FILE=.e2e.json uv run python scripts/seed_e2e.py
docker compose stop maintenance
cd dashboard
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
E2E_SEED_FILE=../.e2e.json pnpm test
```

Playwright starts the dashboard, or reuses an existing local instance outside CI. Browser checks cover key creation, a governed model call, spend visibility, workflow pause, reports, mobile controls, sign out, and foreign-origin mutation rejection.

GitHub Actions runs backend, dashboard, and complete-history credential checks. Mock CI needs no paid provider keys. See [contributor setup](CONTRIBUTING.md) for the review workflow.

### Live acceptance still to run

Use suitable accounts and bounded test prompts to verify:

- Native provider calls and streaming against each real API.
- Interrupted-stream settlement and any provider-specific usage differences.
- Telegram approval, SMTP review links, and signed webhook delivery/decisions.
- Dedicated OpenAI project and Anthropic workspace validation and reporting.
- Reconciliation after provider reporting delays, drift alerts, and dashboard/report totals.

Record results in [acceptance evidence](docs/phase-exits.md). Sanitize live captures before using them as fixtures; omit credentials and real client content.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Docker commands stall or Postgres is unreachable | Start the Docker daemon; check `docker compose ps` and the configured host port |
| Dashboard login fails | Run migrations and the bootstrap script; use the dashboard password rather than `ADMIN_TOKEN` |
| `/v1/models` is empty or a model is rejected | Check the workflow allowlist, provider credential, and effective model price |
| A native call is rejected before forwarding | Inspect its provider-shaped error and `x-gateway-error`; check mapping, budget, rate, and kill state |
| First call in a new month is blocked | Create the workflow's budget for the current UTC month |
| Per-run limit trips too soon | Check the smallest configured cap and whether multiple executions share `X-Run-Id` |
| Paused workflow still accepts a brief call | Kill state can take up to five seconds to propagate |
| Action is denied immediately | Configure its action name and check strict-action policy and risk |
| Approval token is rejected | Check action equality, workflow, expiry, and whether it was already consumed |
| Official provider forwarding fails | Validate the dedicated account mapping and associated inference key |
| Reconciliation shows unknown or failed | Configure organization reporting credentials and inspect the job outcome; allow for reporting delay |
| Budget reservations remain pending | Check the maintenance worker and provider timeouts; stale reservations are reaped after one hour |

Avoid including secrets, approval payloads, or database dumps in public debugging reports. Share redacted error codes and reproduction steps.

## Repository guide

| Path | Contents |
| --- | --- |
| [`gateway/`](gateway/) | Native proxy routes, authentication, provider adapters, outage handling |
| [`policy/`](policy/) | Budgets, shared rate limits, loop breaking, kill flags, reservation/replay maintenance |
| [`approvals/`](approvals/) | Action checks, signing, consumption, expiry, notifications, human decisions |
| [`audit/`](audit/) | Append-only chain creation and verification |
| [`admin_api/`](admin_api/) | Administrative resources, users, dashboard data, monthly reports |
| [`reconcile/`](reconcile/) | Provider reporting, comparison snapshots, drift alerts |
| [`core/`](core/) | Database models, encryption, usage normalization, effective-dated prices |
| [`dashboard/`](dashboard/) | Next.js interface and Playwright acceptance |
| [`sdks/`](sdks/) | Python and JavaScript action wrappers |
| [`migrations/`](migrations/) | Alembic migrations from `0001` through the implemented schema |
| [`mock_provider/`](mock_provider/) | Deterministic provider for local development |
| [`scripts/`](scripts/) | Administrator bootstrap, mock acceptance seed, Telegram setup |
| [`tests/`](tests/) | Backend contracts, Postgres integration, sanitized mock fixtures |
| [`docs/`](docs/) | Decisions, acceptance evidence, pricing, live setup, dashboard, reconciliation, reports |
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | Backend, dashboard, and credential-scanning CI |

## Contributing

Everyone is welcome to contribute through issues and pull requests. Fork the repository, create a focused branch, and follow [CONTRIBUTING.md](CONTRIBUTING.md) for setup, verification, and review expectations. Useful contributions include live contract verification, provider metering, governance edge cases, documentation, and deployment improvements.

Public visibility does not grant direct write access; pull requests let maintainers review contributions. Keep credentials and real client content out of source, fixtures, screenshots, and Git history. For vulnerabilities, follow [SECURITY.md](SECURITY.md).

## License

GovernFlow is available under the [MIT license](LICENSE).
