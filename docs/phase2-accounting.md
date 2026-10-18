# Provider accounting and recovery

Phase 2 implements the accounting and proxy lifecycle in the [agency release brief](agency-release.md). The delivery scope is automatic accounting recovery, evidence-backed administrator corrections through the API, and focused visibility in the existing dashboard. Deployment uses a maintenance window; mixed application versions are unsupported.

## Implementation sequence

1. Add canonical provider calls, immutable charge adjustments and evidence receipts, and leased recovery jobs in migration `0019`.
2. Route live settlement, lease expiry, legacy replay, and corrections through one transactional accounting service.
3. Persist failed settlements in SQLite for self-hosting or S3 for hosted operation; acknowledge evidence only after a matching committed receipt.
4. Bound provider requests, concurrency, buffers, and execution time; reuse application-lifetime HTTP connections and drain outstanding work.
5. Add scoped inspection, retry and correction APIs, plus report projections and dashboard uncertainty/backlog indicators.
6. Verify migration compatibility, recovery ordering, concurrency, SDK/browser contracts, and local mock latency. Record the results in [release status](agency-release-status.md).

## Call and money invariants

The stable call UUID is also its reservation UUID and is returned as `x-gateway-call-id` after reservation. Calls move from `reserved` to `dispatched`, then to `settled_measured`, `settled_estimated`, or `voided`. Dispatch intent commits before the HTTP send. A call stores its agency, client, workflow, run, original budget month, price snapshot, estimate, latest charge/revision, normalized usage, provider request ID, dispatch time, and lease.

An undispatched expired reservation may be voided. A dispatched call is voided only with evidence that the request was not transmitted, or a recognized native rejection. Connection establishment and connection-pool failures are treated as not transmitted. Ambiguous read/write timeouts, disconnects, incomplete responses, and expired dispatch leases retain a conservative estimated charge. Missing usage never implies zero cost. Partial usage can increase the estimate. No accounting worker repeats a provider request.

The accounting transaction locks the call, reservation, budget, and run in a consistent order, then updates balances, appends a charge entry, writes the evidence receipt, and extends the client's audit chain. Budget/run reservations are released once. Charges and receipts are append-only in Postgres, protected by triggers and runtime grants. Every evidence UUID is bound to a canonical content checksum. Repeated identical evidence is idempotent; changed content under the same UUID conflicts.

Complete measured usage may replace an estimate. An administrator correction requires a settled call, current expected revision, a new evidence UUID, a reason, and an evidence reference. Conflicting complete measurements and automatic evidence that contradict an administrator correction require inspection. Corrections adjust the original budget month and latest report projection without adding a second call or changing earlier ledger entries. Existing overrun stops remain until an authorized operator clears them.

Prices are snapshotted before dispatch. Journals contain accounting IDs, normalized counters, amounts, reasons, and the price/ownership seed needed for single-node outage recovery. They exclude prompts, response bodies, provider credentials, session tokens, and encryption/signing secrets.

## Recovery storage and retries

Normal accounting and leased recovery jobs use Postgres. The database clock governs leases and job eligibility. Successful recovery commits the accounting receipt and completed job together. Only then may the external journal entry be acknowledged. A lost acknowledgement safely repeats receipt verification. Transient failures use bounded exponential backoff; jobs are quarantined on permanent evidence conflicts or after ten failed attempts. An explicit scoped retry resets eligibility and attempts. It never executes inference.

For self-hosting, `RECOVERY_BACKEND=sqlite` uses `REPLAY_QUEUE_PATH`, default `/queue/replay.sqlite3`, on a persistent volume. SQLite uses WAL and full synchronous commits. Runtime startup probes the store and refuses temporary-directory storage in the restricted deployment. Preserve both the existing legacy queue and the new evidence table when upgrading or restoring. Take a consistent SQLite backup, including committed WAL contents.

For hosted operation, set:

```dotenv
DEPLOYMENT_MODE=hosted
RECOVERY_BACKEND=s3
RECOVERY_S3_BUCKET=your-accounting-recovery-bucket
RECOVERY_S3_PREFIX=governflow-recovery
AWS_DEFAULT_REGION=us-east-1
ALLOW_FAIL_OPEN=false
```

Use the workload's AWS role. Leave `RECOVERY_S3_ENDPOINT` unset for AWS; it is an emulator/compatible-storage override. Hosted mode refuses SQLite and fail open. Journal writes use server-side AES256 encryption and conditional creation under `{prefix}/v1/{agency}/{call}/{evidence}.json`. Duplicate creation checks exact content. Recovery rechecks object identity/content before conditional deletion using its ETag. Oversized, corrupt, or conflicting objects are retained for inspection. Health probes use a separate `{prefix}/health/` namespace.

Provision bucket access separately from the application. The producer requires `s3:PutObject` on the evidence and health prefixes and `s3:GetObject` on evidence for duplicate verification. The recovery worker additionally requires prefix-scoped `s3:ListBucket`, `s3:GetObject`, and `s3:DeleteObject` on evidence. Give only recovery workers deletion authority. Keep the bucket private, require encrypted transport, and preserve unacknowledged evidence. Health objects may have a short lifecycle; do not expire outstanding evidence on an arbitrary age schedule. Account-specific IAM, bucket policies, AWS deployment, and task-replacement testing remain hosted-release acceptance work.

If both Postgres settlement and journal writing fail, the gateway blocks new governed calls and retries bounded in-memory terminal evidence until storage returns or shutdown ends. The already committed dispatch intent still protects the estimated charge. A hard process/host loss during simultaneous database and journal unavailability cannot preserve terminal measurement held only in memory; its charge remains estimated and needs later evidence. Persistent journals protect evidence once their write succeeds.

## Request bounds and shutdown

| Setting | Default | Behavior |
| --- | --- | --- |
| `MAX_REQUEST_BYTES` | 2 MiB | Rejects oversized declared or chunked requests with native 413 |
| `MAX_RESPONSE_BYTES` | 16 MiB | Bounds buffered provider responses |
| `MAX_TRACKER_BYTES` | 2 MiB | Bounds usage-parser buffering |
| `GATEWAY_MAX_INFLIGHT` | 128 | Per-process admission limit; 429 with retry guidance |
| `WORKFLOW_MAX_INFLIGHT` | 32 | Per-process workflow limit, held until response closes |
| `DEFAULT_MAX_OUTPUT_TOKENS` | 4096 | Injects a missing provider-native output limit |
| `MAX_OUTPUT_TOKENS` | 32768 | Server ceiling; workflow `max_output_tokens` may lower it |
| `CALL_DEADLINE_SECONDS` | 300 s | Total provider call/stream deadline |
| `CALL_LEASE_SECONDS` / `CALL_RENEW_SECONDS` | 60 s / 15 s | Leased dispatch with periodic renewal |
| `DB_POOL_SIZE` | 10 | Short-transaction pool, zero overflow |

Explicit valid output limits preserve request bytes. OpenAI Chat accepts either native limit alias, rejects conflicting values, and uses `max_completion_tokens` when injecting. Responses uses `max_output_tokens`, Anthropic uses `max_tokens`, and Gemini uses `generationConfig.maxOutputTokens`. Multiple candidates are included in the reserved output allowance. Embeddings have no output-token allowance. Input estimation conservatively counts the full UTF-8 request size; unsupported billing categories remain rejected.

HTTP clients live for the application lifespan. No database transaction remains open during provider network waits. Connect/pool timeouts and total deadlines are bounded. Cancellation shields finalization, while response closure releases admission. Compose gives Uvicorn 30 seconds to drain active requests and allows 45 seconds before container termination. Application shutdown also waits up to 30 seconds for tracked accounting tasks. A forced shutdown leaves dispatch leases for conservative recovery.

Single-node fail open additionally requires `SELF_HOSTED_SINGLE_NODE=true`, `ALLOW_FAIL_OPEN=true`, SQLite storage, and administrator-enabled R0/R1 `outage.mode=open`. The 60-second cached allowance is bound to the original run and UTC month and capped by remaining allowance and call count. It cannot receive fresh kill or policy changes during a database outage. Multi-node fail open is unsupported.

## Inspection and correction interfaces

All routes require agency-bound authentication and current client access. Lists default to 50 entries, allow up to 200, accept a UUID `cursor`, and return `X-Next-Cursor` when another page exists.

| Route | Scope and result |
| --- | --- |
| `GET /admin/provider-calls` | Accessible calls; optional `client_id`, `workflow_id`, `state` |
| `GET /admin/provider-calls/{call_id}` | Latest charge, usage/provenance, adjustments, recovery status |
| `POST /admin/provider-calls/{call_id}/corrections` | Administrator; revision-checked measured replacement |
| `GET /admin/accounting-recovery` | Administrator/operator; scoped jobs and sanitized failures |
| `POST /admin/accounting-recovery/{evidence_id}/retry` | Administrator/operator; refuses completed or actively leased jobs |

Correction JSON has `evidence_id` (UUID), `expected_revision` (current revision), `measured_micro` (nonnegative integer), `reason`, and `evidence_ref`. Repeating the same evidence returns the existing outcome. A stale revision or conflicting evidence returns 409. Preserve external evidence in an access-controlled system and reference it; do not place secrets in the note.

The CLI uses `GOVERNFLOW_ADMIN_SESSION` from the environment:

```bash
python -m policy.recovery_cli list --url http://localhost:8001
python -m policy.recovery_cli retry EVIDENCE_UUID --url http://localhost:8001
python -m policy.recovery_cli local-status
```

`local-status` inspects local SQLite metadata. The scoped API shows jobs imported into Postgres; external entries waiting for database restoration and malformed retained S3 objects need storage-level inspection. PostgreSQL job retries do not rewrite corrupt external objects. Investigate identity conflicts before retrying. The dashboard shows estimated/corrected totals and recovery backlog; a dedicated recovery console is outside this phase.

## Maintenance-window upgrade

1. Schedule downtime and stop all old gateway, admin, dashboard, notifier, reconciler, and recovery processes. Let in-flight calls drain. Do not run old writers with the new schema.
2. Back up Postgres and make a consistent backup of the existing replay volume. Preserve original encryption/signing keys and runtime credentials. Retain the old image and verify backups are restorable.
3. Configure persistent SQLite or provision the S3 bucket/roles above. Review output ceilings, concurrency, deadlines, and explicit fail-open switches. Keep the current deployment's private environment intact.
4. Build the new images and run owner initialization once: `docker compose run --rm initialize`. It applies migration `0019`, seeds prices, and provisions the restricted login. Ordinary services continue using `governflow_app` with agency RLS.
5. Start the new stack and recovery worker together. Inspect legacy pending calls, recovery jobs, worker errors, and original-month reports before resuming traffic. Verify the gateway health endpoint and make one controlled mock or approved provider call.

Migration `0019` leaves historical terminal balances and audit payloads unchanged. Legacy pending reservations are adopted as uncertain dispatched calls with expired leases, because old versions did not prove dispatch state; recovery estimates them rather than silently releasing possible spend. Historical terminal reservations are adopted only when queued legacy evidence needs them, using their prior charge as a reporting baseline. Unrepresentable or ambiguous legacy queue entries are retained, not discarded.

After new ledger/recovery activity, downgrade is refused. Roll back by restoring the matching database and journal backups during another maintenance window, then restarting the matching old image. Do not delete ledger evidence to force a downgrade or run old replay logic against the upgraded queue.

## Acceptance boundaries

Local acceptance uses Postgres 16, restricted runtime roles, a Moto 5.2.4 S3 emulator, deterministic provider mocks, official SDK contract tests, and Playwright. It covers reaper/replay ordering, duplicate/conflicting evidence, lost acknowledgement, delayed measurement, original-month correction, legacy migration, concurrent workers, and proxy cancellation/backpressure. The 100-stream test uses a five-connection database pool and verifies zero checked-out connections while all provider streams wait.

`scripts/benchmark_gateway.py` runs a bounded 200-request, 20-rps localhost mock workload and reports direct-provider and gateway latency. It is a development acceptance benchmark, not a production capacity claim. The dated results and remaining live gates are recorded in [release status](agency-release-status.md). Real AWS permissions/task replacement, live provider charges, and real notification delivery remain later release gates.
