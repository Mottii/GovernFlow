"""Normalize native output limits before both reservation and transmission."""

import json
from typing import Any

from gateway.auth import AuthContext
from gateway.errors import GatewayError


def output_limit(body: bytes, provider: str, endpoint: str, auth: AuthContext) -> tuple[bytes, int]:
    parsed: dict[str, Any] = json.loads(body)
    if endpoint == "/v1/embeddings":
        return body, 0
    from gateway.runtime import positive_setting

    global_ceiling = positive_setting("MAX_OUTPUT_TOKENS", 32768)
    policy_ceiling = auth.policy.get("max_output_tokens", global_ceiling)
    if (
        isinstance(policy_ceiling, bool)
        or not isinstance(policy_ceiling, int)
        or policy_ceiling <= 0
    ):
        raise GatewayError(503, "invalid_policy", "Workflow output ceiling must be positive")
    ceiling = min(global_ceiling, policy_ceiling)
    default = min(positive_setting("DEFAULT_MAX_OUTPUT_TOKENS", 4096), ceiling)
    target = parsed
    if provider == "gemini":
        target = parsed.setdefault("generationConfig", {})
        field = "maxOutputTokens"
        multiple = target.get("candidateCount", 1)
    else:
        field = (
            "max_tokens"
            if provider == "anthropic"
            else "max_output_tokens"
            if endpoint == "/v1/responses"
            else "max_completion_tokens"
        )
        multiple = parsed.get("n", 1)
    if provider == "openai" and endpoint == "/v1/chat/completions":
        if (
            "max_completion_tokens" in parsed
            and "max_tokens" in parsed
            and parsed["max_completion_tokens"] != parsed["max_tokens"]
        ):
            raise GatewayError(400, "conflicting_output_limits", "Output token limits disagree")
        if "max_tokens" in parsed and "max_completion_tokens" not in parsed:
            field = "max_tokens"
    value = target.get(field, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= ceiling
        or isinstance(multiple, bool)
        or not isinstance(multiple, int)
        or not 0 < multiple <= 128
    ):
        raise GatewayError(400, "invalid_max_tokens", "Output limit exceeds policy or is invalid")
    if field in target:
        return body, value * multiple
    target[field] = value
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode(), value * multiple
