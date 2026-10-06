# Daily reconciliation

Run `uv run python -m reconcile.service` for the preceding three UTC days, or add `--day YYYY-MM-DD`. Compose runs this every 24 hours. The lookback refreshes delayed provider reporting; job failures remain visible and preserve the last successful snapshot.

Each client uses a dedicated OpenAI project and a dedicated Anthropic workspace. Create encrypted inference credentials, then POST `/admin/provider-accounts` with `client_id`, `provider`, `external_id`, and `credential_label`. Anthropic also requires `inference_key_id`, the API key's metadata ID. The default `validate_live: true` checks the account with the organization reporting key. `validate_live: false` supports mock fixtures and is insufficient for forwarding to official provider hosts.

Organization reporting keys live only in environment variables or a secret manager. They are not the inference keys. See [credential setup](live-setup.md).

## Comparisons

- OpenAI: model-level input/output/cache usage within the mapped project; total USD costs grouped by project and line item. The cost API does not expose model-level costs.
- Anthropic: model-level input/output/cache usage within the mapped workspace; total costs grouped by workspace and description, retaining provider metadata. Amounts are decimal cents and are converted to micro-USD.
- Gemini: gateway-observed usage and cost only in v1.

Provider usage includes cache tokens in total input; adapters normalize gateway events to the same convention. Prices and billing options can explain cost differences even when token totals match. Estimated or interrupted calls have unknown measured usage and are identified separately. UTC provider request-day and gateway settlement-day may differ near midnight. Provider delays and unsupported billing categories can also cause drift; a positive unexplained difference is evidence for investigation, not proof of a bypass.

Official contracts: [OpenAI usage](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/completions), [OpenAI costs](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs), [Anthropic usage and costs](https://platform.claude.com/docs/en/manage-claude/usage-cost-api). `tests/fixtures/reporting.json` is a deterministic mock following these schemas, not a live capture.

## Drift alerts

Every successful refresh compares input/output counts by model and account-level cost. Defaults allow one micro-USD of cost rounding and no unexplained token difference; `DRIFT_COST_TOLERANCE_MICRO` and `DRIFT_TOKEN_TOLERANCE` override these thresholds. Positive unexplained drift is labeled **possible bypass** immediately. Estimated gateway calls instead produce **drift requires review**, because their measured token usage is unknown. Negative differences also require review.

Alerts are deduplicated per client, provider, and day, carry the actual differences, and resolve when a later refresh matches. Detection, reclassification, reopening, and resolution enter the audit chain. A failed reporting job does not detect drift against stale snapshots. These are investigation flags: provider lag and UTC settlement boundaries can produce temporary differences.
