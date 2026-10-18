import json
import os
from typing import Any

from core.usage import Usage


def chat_usage(body: dict[str, Any]) -> Usage | None:
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return None
    if any(
        isinstance(raw.get(name), bool) or not isinstance(raw.get(name), int) or raw[name] < 0
        for name in ("prompt_tokens", "completion_tokens")
    ):
        return None
    input_total = raw["prompt_tokens"]
    output_total = raw["completion_tokens"]
    input_details = raw.get("prompt_tokens_details") or {}
    output_details = raw.get("completion_tokens_details") or {}
    if not isinstance(input_details, dict) or not isinstance(output_details, dict):
        return None
    cached = input_details.get("cached_tokens", 0)
    reasoning = output_details.get("reasoning_tokens", 0)
    cache_write = input_details.get("cache_write_tokens", 0)
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (cached, reasoning, cache_write)
        )
        or cached + cache_write > input_total
        or reasoning > output_total
    ):
        return None
    return Usage(
        input=max(0, input_total - cached - cache_write),
        output=max(0, output_total - reasoning),
        cache_read=cached,
        cache_write=cache_write,
        reasoning=reasoning,
        raw=raw,
    )


class ChatStreamTracker:
    def __init__(self, suppress_injected_usage: bool) -> None:
        self.suppress_injected_usage = suppress_injected_usage
        self.buffer = bytearray()
        self._usage: Usage | None = None
        self._complete = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer.extend(chunk)
        if len(self.buffer) > int(os.environ.get("MAX_TRACKER_BYTES", str(2 * 1024 * 1024))):
            raise ValueError("Provider frame exceeds accounting buffer limit")
        ready: list[bytes] = []
        while True:
            lf = self.buffer.find(b"\n\n")
            crlf = self.buffer.find(b"\r\n\r\n")
            if lf < 0 and crlf < 0:
                break
            if lf >= 0 and (crlf < 0 or lf < crlf):
                end = lf + 2
            else:
                end = crlf + 4
            frame = bytes(self.buffer[:end])
            del self.buffer[:end]
            payload = b"\n".join(
                line.partition(b":")[2].lstrip()
                for line in frame.splitlines()
                if line.startswith(b"data:")
            )
            if payload == b"[DONE]":
                self._complete = True
            usage_chunk = False
            if payload and payload != b"[DONE]":
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        usage = chat_usage(parsed)
                        if usage is not None:
                            self._usage = usage
                            usage_chunk = parsed.get("choices") == []
                except ValueError:
                    pass
            if not (self.suppress_injected_usage and usage_chunk):
                ready.append(frame)
        return ready

    @property
    def usage(self) -> Usage | None:
        return self._usage if self._complete else None

    @property
    def partial_usage(self) -> Usage | None:
        return self._usage

    def flush(self) -> bytes:
        remaining = bytes(self.buffer)
        self.buffer.clear()
        return remaining
