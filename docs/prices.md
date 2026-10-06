# Price catalog

Verified from official pages on 2026-10-06. Values in `core/model_prices.json` are integer micro-USD per **one million tokens**. Run `python -m core.pricing` or POST `/admin/prices/seed`. Seed insertion is idempotent and does not replace a price already recorded for that effective date.

| Provider/model | Input USD | Output USD | Cache read USD | Cache write USD |
|---|---:|---:|---:|---:|
| [OpenAI gpt-4.1-mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) | 0.40 | 1.60 | 0.10 | 0 |
| [OpenAI gpt-6-sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | 2.00 | 10.00 | 0.20 | 2.50 |
| [OpenAI text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small) | 0.02 | 0 | 0 | 0 |
| [Anthropic claude-haiku-4-5](https://platform.claude.com/docs/en/about-claude/pricing) | 1.00 | 5.00 | 0.10 | 1.25 / 2.00 for 1h |
| [Anthropic claude-sonnet-5-5](https://platform.claude.com/docs/en/about-claude/pricing) | 2.00 | 10.00 | 0.20 | 2.50 / 4.00 for 1h |
| [Gemini gemini-3.5-flash-lite](https://ai.google.dev/gemini-api/docs/pricing) | 0.30 | 2.50 | 0.03 | 0 |

Reasoning uses the output rate. GPT-6 Sol input above 272,000 tokens applies 2x input/cache and 1.5x output to the whole request. Fractions are calculated exactly before rounding the final micro-USD charge upward. Anthropic cache writes use the reported 5-minute and 1-hour split.

These seed entries meter standard token billing. Hosted provider tools, alternate service tiers, audio/video, generated media, and background Responses are rejected until their meters are configured. Function tools preserve their native provider shape. Keep provider project defaults on standard processing. Gemini cache-storage charges are outside these proxy routes and remain provider-side charges; Gemini has no billing reconciliation in v1.

Administrators can add an effective-dated price with `/admin/prices`. It requires a source URL and records a UTC verification timestamp. Missing prices fail closed. Reverify prices before changing model access or deploying against a provider's live service.
