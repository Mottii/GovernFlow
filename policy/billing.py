"""Reject billing options absent from a standard token price entry."""

import json
from typing import Any

from core.models import Price
from gateway.errors import GatewayError


def standard_meter(body: bytes, provider: str, price: Price) -> None:
    if not (price.meter or {}).get("standard_only"):
        return
    parsed = json.loads(body)
    invalid = parsed.get("service_tier") not in (None, "auto", "default", "standard")
    invalid |= parsed.get("speed") == "fast" or parsed.get("background") is True
    invalid |= parsed.get("inference_geo") not in (None, "global")
    tools = parsed.get("tools", [])
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue  # The native provider validates its own request schema.
            if provider == "gemini":
                invalid |= any(key != "functionDeclarations" for key in tool)
            else:
                invalid |= tool.get("type") not in (None, "function", "custom")
    config = parsed.get("generationConfig") or {}
    if isinstance(config, dict):
        invalid |= any(mode != "TEXT" for mode in config.get("responseModalities", []))
    invalid |= any(mode != "text" for mode in parsed.get("modalities", []))

    def audio_video(value: Any) -> bool:
        if isinstance(value, dict):
            mime = value.get("mimeType", value.get("mime_type", ""))
            return (
                isinstance(mime, str)
                and mime.startswith(("audio/", "video/"))
                or "input_audio" in value
                or any(audio_video(item) for item in value.values())
            )
        return isinstance(value, list) and any(audio_video(item) for item in value)

    invalid |= audio_video(parsed)
    if invalid:
        raise GatewayError(
            400, "billing_option_not_priced", "This billing option has no configured meter"
        )
