import json
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


class ChatStreamTracker:
    def __init__(self, suppress_injected_usage: bool) -> None:
        self.suppress_injected_usage = suppress_injected_usage
        self.buffer = bytearray()
        self.usage: Usage | None = None

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer.extend(chunk)
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
            usage_chunk = False
            if payload and payload != b"[DONE]":
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        usage = chat_usage(parsed)
                        if usage is not None:
                            self.usage = usage
                            usage_chunk = parsed.get("choices") == []
                except ValueError:
                    pass
            if not (self.suppress_injected_usage and usage_chunk):
                ready.append(frame)
        return ready

    def flush(self) -> bytes:
        remaining = bytes(self.buffer)
        self.buffer.clear()
        return remaining
