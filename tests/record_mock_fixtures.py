"""Record deterministic mock fixtures; schema references are in the manifest."""

import asyncio
import json
from pathlib import Path

import httpx

from mock_provider.app import app


async def main():
    cases = [
        (
            "chat",
            "/v1/chat/completions",
            {
                "model": "gpt-mock",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "responses",
            "/v1/responses",
            {"model": "gpt-mock", "max_output_tokens": 1024, "input": "hello"},
        ),
        ("embeddings", "/v1/embeddings", {"model": "text-embedding-mock", "input": "hello"}),
        (
            "messages",
            "/v1/messages",
            {
                "model": "claude-mock",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "gemini",
            "/v1beta/models/gemini-mock:generateContent",
            {
                "contents": [{"parts": [{"text": "hello"}]}],
                "generationConfig": {"maxOutputTokens": 1024},
            },
        ),
    ]
    root = Path(__file__).parent / "fixtures"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as http:
        for name, path, body in cases:
            response = await http.post(path, json=body)
            (root / (name + ".json")).write_text(json.dumps(response.json(), indent=2) + "\n")
            if name == "embeddings":
                continue
            stream_path = path.replace(":generateContent", ":streamGenerateContent?alt=sse")
            response = await http.post(
                stream_path,
                json={**body, "stream": True, "stream_options": {"include_usage": True}},
            )
            (root / (name + ".sse")).write_bytes(response.content)
    manifest = {
        "recorded": "2026-10-06",
        "origin": "local deterministic mock, no live provider calls",
        "references": [
            "https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events",
            "https://developers.openai.com/api/reference/resources/responses/streaming-events",
            "https://developers.openai.com/api/reference/resources/embeddings/methods/create",
            "https://platform.claude.com/docs/en/api/messages/create",
            "https://platform.claude.com/docs/en/build-with-claude/streaming",
            "https://ai.google.dev/api/generate-content",
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
