from typing import Any

from core.usage import Usage


def chat_usage(body: dict[str, Any]) -> Usage | None:
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return None
    input_total = int(raw.get("prompt_tokens", 0))
    output_total = int(raw.get("completion_tokens", 0))
    cached = int((raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    reasoning = int((raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
    return Usage(
        input=max(0, input_total - cached),
        output=max(0, output_total - reasoning),
        cache_read=cached,
        reasoning=reasoning,
        raw=raw,
    )
