import json
import os
from typing import Any

from core.usage import Usage


def content_usage(body: dict[str, Any]) -> Usage | None:
    raw = body.get("usageMetadata")
    if not isinstance(raw, dict) or "promptTokenCount" not in raw:
        return None
    names = (
        "promptTokenCount",
        "candidatesTokenCount",
        "cachedContentTokenCount",
        "thoughtsTokenCount",
        "toolUsePromptTokenCount",
    )
    values = [raw.get(name, 0) for name in names]
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        return None
    prompt, output, cached, thoughts, tool_input = values
    if cached > prompt:
        return None
    return Usage(
        input=prompt - cached + tool_input,
        output=output,
        cache_read=cached,
        reasoning=thoughts,
        raw=raw,
    )


class ContentStreamTracker:
    def __init__(self, sse: bool) -> None:
        self._sse = sse
        self._buffer = bytearray()
        self._usage: Usage | None = None
        self._complete = False

    def _observe(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        usage = content_usage(event)
        if usage is not None:
            self._usage = usage
        for candidate in event.get("candidates", []):
            if isinstance(candidate, dict) and candidate.get("finishReason"):
                self._complete = True

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        if len(self._buffer) > int(os.environ.get("MAX_TRACKER_BYTES", str(2 * 1024 * 1024))):
            raise ValueError("Provider frame exceeds accounting buffer limit")
        if not self._sse:
            return
        while True:
            lf, crlf = self._buffer.find(b"\n\n"), self._buffer.find(b"\r\n\r\n")
            if lf < 0 and crlf < 0:
                return
            end = lf + 2 if lf >= 0 and (crlf < 0 or lf < crlf) else crlf + 4
            frame = bytes(self._buffer[:end])
            del self._buffer[:end]
            data = b"\n".join(
                line.partition(b":")[2].lstrip()
                for line in frame.splitlines()
                if line.startswith(b"data:")
            )
            try:
                self._observe(json.loads(data))
            except (ValueError, UnicodeDecodeError):
                pass

    @property
    def partial_usage(self) -> Usage | None:
        return self._usage

    @property
    def usage(self) -> Usage | None:
        if not self._sse:
            try:
                events = json.loads(bytes(self._buffer))
                for event in events if isinstance(events, list) else [events]:
                    self._observe(event)
            except (ValueError, UnicodeDecodeError):
                return None
        return self._usage if self._complete else None
