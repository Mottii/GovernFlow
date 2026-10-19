# Phase 3 agency controls

Phase 3 extends provider accounting with shared client allowances, recurring UTC budgets, run-scoped stops, immutable policy revisions, credential versions, and deployment key rings. The existing onboarding flow remains separate from the Phase 4 wizard. Public run lifecycle and business-action outcomes remain Phase 5.

## Client budgets and renewal

Every reservation, settlement, late measurement, and correction updates the client, workflow, and run together. Client balances include every workflow's existing spend and outstanding reservations in that UTC month. A missing client cap means tracking without a client limit; workflow budgets are still required. Adding a client cap never resets existing balances. A cap below spend plus reservations blocks further admission until sufficient room exists.

Client buckets migrate from existing workflow balances without introducing caps. Existing budgets remain one-off. The Governance dashboard defaults new budget forms to recurring monthly budgets, with the choice visible. Saving changes the selected month's cap and, when recurrence is checked, schedules future months from the next UTC month. Those are separate audited operations; if the schedule fails after a cap update, the cap remains saved and the error is displayed.

Monthly templates are immutable revisions. Their default effective date is next month; an explicit current-month template may create a missing bucket, but it never overwrites an existing bucket. Change the current cap explicitly. Disabling future renewal leaves existing buckets intact. The worker and admission path both materialize the current month idempotently, without carrying allowance forward or creating missed historical months.

| Interface | Purpose |
| --- | --- |
| `GET /admin/client-budgets?period=YYYY-MM` | Accessible client balances, cap, headroom, revision, and template source |
| `PUT /admin/clients/{id}/budget` | Create/update a client cap using `period`, nullable `cap_micro`, `expected_revision`, and `reason` |
| `POST /admin/budgets` | Existing one-off workflow budget creation |
| `PATCH /admin/budgets/{id}` | Workflow cap update using `cap_micro`, `expected_revision`, and `reason` |
| `GET /admin/budget-templates` | Accessible template history |
| `POST /admin/budget-templates` | Append a template revision: `scope`, `scope_id`, `cap_micro`, `enabled`, `expected_revision`, optional `effective_period`, and `reason` |

Amounts are integer micro-USD in the API. The dashboard accepts USD with at most six decimal places. New client buckets start at revision 1. Template history starts at revision 1; use expected revision 0 to create its first entry. Stale changes return 409. Administrators and scoped operators may change budgets; viewers and approvers have read access.

Dashboard spend and monthly JSON reports include client cap/headroom, current run-stop reasons, and the latest renewal templates with their effective UTC period. Stop/schedule metadata describes current controls even when the report selects a historical accounting month; balances remain specific to the selected month. Existing list response bodies remain unchanged.

Single-node fail-open mode retains its explicit R0/R1 opt-in and 60-second stale-state limitation. Cached routes share client/workflow/run allowances rather than cloning remaining client money. Outstanding local dispatch intents debit new caches conservatively. Old journal records remain readable without changing their receipt checksums.

## Run stops

Run-cost or step exhaustion and reservation overruns stop the offending run. Full provider charges remain accounted for, even above a reservation or cap. Sibling runs remain eligible unless a wider budget is exhausted or policy explicitly escalates the stop. `stop_escalation` is `run` by default and may be set to `workflow` or `client` by a published policy.

Supply a stable `X-Run-Id` for each execution. Without one, the existing ten-minute workflow window acts as the run, so calls in that window share its stop. Phase 3 does not add public run-start/completion APIs.

`GET /admin/run-stops` lists accessible stopped runs. `PUT /admin/runs/{id}/stop` accepts `active`, `expected_revision`, and a reason. Clearing preserves all money and step counters, requires room under current limits, and updates the run cap to the approved current policy. Raise exhausted policy/budget limits before clearing a stop. Downward financial corrections do not automatically resume a run. Emergency global/client/workflow controls remain separate. Ordinary RPM exhaustion returns temporary 429 backpressure.

## Guided policies

Use Governance → Policy builder to select a workflow, edit settings, add named rules and conditions, try a sample action, and save an immutable draft. Operators can draft and preview. Only an agency administrator can activate or roll back. New operator-created workflows remain blocked until their first usable policy is published.

Settings include separate request RPM and loop request limits, run steps/cost, output-token ceiling, stop escalation, assigned approval channels, and outage mode. Operators can propose fail-open settings in a draft; only administrator publication makes them active. Deployment opt-in is still required, and R2/R3 must stay closed.

A rule has a stable ID, exact `action_name`, outcome (`deny`, `review`, or `allow`), conditions, optional risk floor, review timeout, and timeout outcome. Every condition in a rule must match; several rules provide alternatives. Deny has priority over review, then allow. Unmatched actions deny. R2/R3 always require human review and cannot allow on timeout.

Paths use JSON Pointer, such as `/amount` or `/recipients/0`. Conditions support:

- `exists`: boolean comparison value; distinguishes absent fields from present null values.
- `eq`: typed scalar equality; booleans do not equal numbers.
- `in`: membership in a nonempty literal list of scalars.
- `lt`, `lte`, `gt`, `gte`: finite JSON numbers compared as decimal values; numeric strings and booleans are rejected.
- `recipient_domain_in`: a domain list, applied to every plain email address in a string or array. Exact domain matching is the default; `include_subdomains: true` permits subdomains. Empty/malformed recipients fail closed.

Missing or mistyped comparison operands deny the action with a reason, including when another rule would allow it. Nested conditions, scripts, regular expressions, and remote lookups are unsupported. A rule with no conditions matches its named action. The builder's comparison field accepts JSON literals, for example `100`, `true`, `"paid"`, or `["example.test"]`.

Example policy:

```json
{
  "models": ["gpt-mock"],
  "per_run_micro": 1000000,
  "rules": [
    {"id": "large-send", "action_name": "send", "outcome": "review", "conditions": [
      {"path": "/amount", "op": "gte", "value": 100}
    ]},
    {"id": "small-send", "action_name": "send", "outcome": "allow", "conditions": [
      {"path": "/amount", "op": "lt", "value": 100},
      {"path": "/recipient", "op": "recipient_domain_in", "value": ["example.test"]}
    ]}
  ]
}
```

| Interface | Purpose |
| --- | --- |
| `PUT /admin/workflows/{id}/policy` | Save draft with `expected_active_version`, `risk_class`, `policy`, and `reason` |
| `GET /admin/workflows/{id}/policy-revisions` | Active version, review flag, and revision history |
| `POST /admin/workflows/{id}/policy-revisions/{version}/activate` | Administrator publication with expected active version and reason |
| `POST /admin/workflows/{id}/policy-revisions/{version}/rollback` | Publish an earlier policy as a new revision |
| `POST /admin/policies/preview` | Validate/evaluate a candidate using workflow ID, risk class, action name, and sample action |

Preview uses the live evaluator but produces no approval, authorization, notification, reservation, or audit mutation. It returns the normalized policy, effective risk, matching rule IDs, outcome, and explanation. Stale publication returns 409 with current active version metadata. Rollback never reuses an old version number.

Activation invalidates outstanding approvals and all unconsumed authorizations from prior revisions, including timeout allowances, and cancels queued notifications. Polling/consumption reports `policy_changed`; callers must check again. Human decisions and every callback recheck the active revision. Work already consumed is not recalled, and provider calls already admitted retain their captured revision.

Migration converts valid legacy settings/named action rules into active revisions, consolidating run-limit aliases to the smallest configured cap. Unknown actions deny. Malformed policies become blocked with an administrative review flag; the original policy is retained in the migration revision's `legacy_source` for inspection. Existing encrypted approval content remains readable.

## Provider credential versions

The existing client/provider/label row is a stable credential slot. Versions are staged, active, or retired. Listing returns metadata only. A staged replacement is encrypted immediately; activation atomically retires the prior active version and moves the slot pointer. Calls admitted before activation retain their version. Retiring the current version without a replacement disables new calls for that slot, while external provider revocation remains the account owner's responsibility.

| Interface | Purpose |
| --- | --- |
| `POST /admin/provider-credentials` | Create slot and first active version |
| `GET /admin/provider-credentials` | Accessible slot/version metadata; no stored plaintext |
| `POST /admin/provider-credentials/{id}/versions` | Stage `key` with expected latest version and reason |
| `POST /admin/provider-credentials/{id}/versions/{version}/activate` | Activate with `expected_active_version` and reason |
| `POST /admin/provider-credentials/{id}/versions/{version}/retire` | Retire with the same active-version precondition |
| `PUT /admin/provider-accounts` | Revalidate an existing mapping without moving its provider scope or credential slot |

Activation invalidates the credential version's provider-account validation binding. Strict mapped routes stay blocked until revalidation succeeds using the existing provider validation flow. Ordinary inference-only onboarding is a Phase 4 change. Existing validated mappings are bound to their migrated active credential version.

## Deployment key rings and re-encryption

`MASTER_ENCRYPTION_KEY` and `DECISION_SIGNING_KEY` remain supported as key ID `0000`. Optional `ENCRYPTION_KEYS` and `SIGNING_KEYS` are JSON maps from four-digit lowercase hexadecimal IDs to base64-encoded 32-byte secrets. `ACTIVE_ENCRYPTION_KEY_ID` and `ACTIVE_SIGNING_KEY_ID` default to `0000`. Compose passes these only to services that require them. Do not commit key material or change a key's bytes while reusing its ID.

New ciphertext records carry an encryption key ID. Key ID `0000` retains the legacy associated-data format. Other IDs bind the format/version and key ID as well as the existing resource ownership context. New decision/review tokens carry a signed `kid`; new Telegram callbacks carry a compact key ID while staying within the callback size limit. Old formats verify only with retained key `0000`.

Rotation requires an explicit owner maintenance process (`MAINTENANCE_MODE=true`):

1. Back up the database/journals and original key material. Add a new unique key ID to the appropriate ring on every relevant service, retaining old keys.
2. Drain and stop writers/signers during maintenance. Switch the active key IDs in deployment secrets; preserve every retained decryption/verification key.
3. Run `python -m core.rotation preflight`. It checks active key availability, ciphertext references, protected verification keys, and recorded fingerprints. A previously active signing key remains protected before retirement has been observed. Upgrades with existing approvals also retain a legacy `0000` verification marker so pre-upgrade links cannot lose their verifier.
4. Run `python -m core.rotation observe --operator YOUR_OPERATOR_ID` after the last old-key signer has stopped. Initialization also performs preflight/observation. Retired signing keys are protected for seven days plus five minutes from observation.
5. Run `python -m core.rotation reencrypt --batch 100 --operator YOUR_OPERATOR_ID` repeatedly until it reports zero changed rows. Each committed batch can be resumed after interruption; it covers credential slots/versions, encrypted approval content, and integration secrets without changing their plaintext or ownership.
6. Check removal eligibility with `python -m core.rotation eligible --kind encryption --key-id OLD_ID` or `--kind signing`. Encryption removal requires zero remaining references; signing removal requires the retention deadline to pass. Active keys cannot be removed.
7. Update deployment secrets during maintenance only after eligibility passes, rerun preflight, restart, and verify controlled mock/provider and approval flows.

Application roles cannot rewrite credential version identity or ciphertext. Owner maintenance may re-encrypt and records agency audit events. Retirement commands assess application state; backups encrypted with old keys still require those keys for restoration. Preserve them in the backup's controlled recovery material. Key rings are deployment-wide; AWS KMS integration remains deferred.

## Audit and upgrade

`GET /admin/administrative-events` provides administrator-only agency history, with UUID resource references, authenticated actors, allowlisted change metadata, and an append-only hash chain. It supports `cursor` and `limit` (default 50, maximum 200) with `X-Next-Cursor`. Permission, reviewer assignment, policy, budget, integration, credential, and stop mutations are audited transactionally. Stored credentials, passwords, token material, and submitted business-action bodies are excluded. Existing client audit chains remain intact; checkpointing and retention remain later work.

Upgrade from `0019` through `0020` during a maintenance window with all old writers/workers stopped. Back up Postgres, persistent recovery journals, and key material. Run owner initialization once, then start new applications/workers together. Inspect policy review flags, shared balances, credential versions, renewal, and pending recovery before resuming traffic. New tenant tables use agency RLS and matching ownership constraints; ordinary services retain the restricted login.

Populated Phase 3 downgrade is refused. Restore matching database/journal backups and the matching application/key configuration to roll back. Existing Phase 2 recovery payloads remain readable without changing their evidence identity or receipt checksum. Engineering acceptance uses deterministic provider/transport mocks and a local S3 emulator; live external and hosted acceptance remain separate gates.
