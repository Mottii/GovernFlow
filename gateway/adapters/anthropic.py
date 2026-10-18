"""Usage extraction for the native Anthropic Messages dialect."""

import json
import os
from typing import Any

from core.usage import Usage


def message_usage(body: dict[str, Any]) -> Usage | None:
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return None
    if "input_tokens" not in raw or "output_tokens" not in raw:
        return None
    names = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    if any(
        isinstance(raw.get(name, 0), bool)
        or not isinstance(raw.get(name, 0), int)
        or raw.get(name, 0) < 0
        for name in names
    ):
        return None
    cache = raw.get("cache_creation") or {}
    if not isinstance(cache, dict):
        return None
    hour = cache.get("ephemeral_1h_input_tokens", 0)
    if (
        isinstance(hour, bool)
        or not isinstance(hour, int)
        or not 0 <= hour <= raw.get("cache_creation_input_tokens", 0)
    ):
        return None
    return Usage(
        input=raw.get("input_tokens", 0),
        output=raw.get("output_tokens", 0),
        cache_read=raw.get("cache_read_input_tokens", 0),
        cache_write=raw.get("cache_creation_input_tokens", 0) - hour,
        cache_write_hour=hour,
        raw=raw,
    )


class MessageStreamTracker:
    def __init__(self) -> None:
        self._buffer = b""
        self._usage: dict[str, Any] = {}
        self._complete = False
        self._errored = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if len(self._buffer) > int(os.environ.get("MAX_TRACKER_BYTES", str(2 * 1024 * 1024))):
            raise ValueError("Provider frame exceeds accounting buffer limit")
        while True:
            lf, crlf = self._buffer.find(b"\n\n"), self._buffer.find(b"\r\n\r\n")
            if lf < 0 and crlf < 0:
                break
            end = lf + 2 if lf >= 0 and (crlf < 0 or lf < crlf) else crlf + 4
            frame, self._buffer = self._buffer[:end], self._buffer[end:]
            data = b"\n".join(
                line.removeprefix(b"data:").lstrip()
                for line in frame.splitlines()
                if line.startswith(b"data:")
            )
            if not data:
                continue
            try:
                event = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "message_start":
                message = event.get("message")
                if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                    self._usage.update(message["usage"])
            elif event_type == "message_delta" and isinstance(event.get("usage"), dict):
                self._usage.update(event["usage"])
            elif event_type == "message_stop":
                self._complete = True
            elif event_type == "error":
                self._errored = True

    @property
    def partial_usage(self) -> Usage | None:
        return message_usage({"usage": self._usage})

    @property
    def usage(self) -> Usage | None:
        if not self._complete or self._errored:
            return None
        return message_usage({"usage": self._usage})
