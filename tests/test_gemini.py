import json

import pytest
import respx
from sqlalchemy import select

from core.models import Budget
from gateway.adapters.gemini import ContentStreamTracker, content_usage


def test_gemini_normalizes_thoughts_and_cache():
    usage = content_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 20,
                "candidatesTokenCount": 30,
                "thoughtsTokenCount": 10,
            }
        }
    )
    assert usage is not None
    assert (usage.input, usage.cache_read, usage.output, usage.reasoning) == (80, 20, 30, 10)


@pytest.mark.asyncio
async def test_gemini_plain_http_native_contract(governed):
    body = b'{"contents":[{"parts":[{"text":"hello"}]}],"generationConfig":{"maxOutputTokens":16}}'
    result = {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "mock reply"}]}}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
    }
    payload = json.dumps(result).encode()
    with respx.mock(assert_all_called=True) as mock:
        normal = mock.post(
            "http://provider.test/v1beta/models/gemini-mock:generateContent"
        ).respond(200, content=payload)
        sse = mock.post(
            "http://provider.test/v1beta/models/gemini-mock:streamGenerateContent?alt=sse"
        ).respond(
            200,
            content=b"data: " + payload + b"\n\n",
            headers={"content-type": "text/event-stream"},
        )
        array = mock.post(
            "http://provider.test/v1beta/models/gemini-mock:streamGenerateContent"
        ).respond(200, content=b"[" + payload + b"]", headers={"content-type": "application/json"})
        for path, expected in (
            ("generateContent", payload),
            ("streamGenerateContent?alt=sse", b"data: " + payload + b"\n\n"),
            ("streamGenerateContent", b"[" + payload + b"]"),
        ):
            response = await governed.http.post(
                "/v1beta/models/gemini-mock:" + path,
                content=body,
                headers={"x-goog-api-key": governed.a.key},
            )
            assert response.status_code == 200
            assert response.content == expected
        for route in (normal, sse, array):
            assert route.calls[0].request.content == body
            assert route.calls[0].request.headers["x-goog-api-key"] == "provider-A"
    async with governed.factory() as session:
        budget = (
            await session.execute(
                select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
            )
        ).scalar_one()
        assert (budget.spent_micro, budget.reserved_micro) == (45, 0)
    tracker = ContentStreamTracker(True)
    tracker.feed(b'data: {"usageMetadata":{"promptTokenCount":12}}\n\n')
    assert tracker.usage is None
