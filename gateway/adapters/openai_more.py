"""Usage extraction for OpenAI Responses and embeddings."""

import json
from typing import Any

from core.usage import Usage


def _nonnegative(raw: dict[str, Any], name: str) -> int | None:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def responses_usage(body: dict[str, Any]) -> Usage | None:
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return None
    input_total = _nonnegative(raw, "input_tokens")
    output_total = _nonnegative(raw, "output_tokens")
    input_details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    if input_total is None or output_total is None:
        return None
    if not isinstance(input_details, dict) or not isinstance(output_details, dict):
        return None
    cached = input_details.get("cached_tokens", 0)
    cache_write = input_details.get("cache_write_tokens", 0)
    reasoning = output_details.get("reasoning_tokens", 0)
    if any(
        isinstance(v, bool) or not isinstance(v, int) or v < 0
        for v in (cached, cache_write, reasoning)
    ):
        return None
    if cached + cache_write > input_total or reasoning > output_total:
        return None
    return Usage(
        input=input_total - cached - cache_write,
        output=output_total - reasoning,
        cache_read=cached,
        cache_write=cache_write,
        reasoning=reasoning,
        raw=raw,
    )


def embeddings_usage(body: dict[str, Any]) -> Usage | None:
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return None
    prompt_tokens = _nonnegative(raw, "prompt_tokens")
    if prompt_tokens is None:
        return None
    return Usage(input=prompt_tokens, raw=raw)


class ResponsesStreamTracker:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self.usage: Usage | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            lf = self._buffer.find(b"\n\n")
            crlf = self._buffer.find(b"\r\n\r\n")
            if lf < 0 and crlf < 0:
                return
            if lf >= 0 and (crlf < 0 or lf < crlf):
                end = lf + 2
            else:
                end = crlf + 4
            frame = bytes(self._buffer[:end])
            del self._buffer[:end]
            data = b"\n".join(
                line.partition(b":")[2].lstrip()
                for line in frame.splitlines()
                if line.startswith(b"data:")
            )
            if not data:
                continue
            try:
                event = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") not in (
                "response.completed",
                "response.incomplete",
            ):
                continue
            response = event.get("response")
            if isinstance(response, dict):
                self.usage = responses_usage(response)
