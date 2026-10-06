import copy
import json
from pathlib import Path

import anthropic
import httpx
import httpx2
import openai
import pytest
import respx
from sqlalchemy import select, update

from core.models import Budget, Event
from gateway.app import app
from tests.test_native_errors import ROUTES, headers

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_official_sdk_contracts_recorded_fixtures(governed):
    async with governed.factory() as session:
        await session.execute(update(Budget).values(cap_micro=10000000))
        await session.commit()
    oa = openai.AsyncOpenAI(
        api_key=governed.a.key,
        base_url="http://gateway.test/v1",
        http_client=governed.http,
        max_retries=0,
    )
    an_http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test"
    )
    an = anthropic.AsyncAnthropic(
        api_key=governed.a.key,
        base_url="http://gateway.test",
        http_client=an_http,
        max_retries=0,
    )
    with respx.mock(assert_all_called=False) as mock:
        for name, endpoint in [
            ("chat", "chat/completions"),
            ("responses", "responses"),
            ("embeddings", "embeddings"),
            ("messages", "messages"),
        ]:

            def reply(request, fixture=name):
                if json.loads(request.content).get("stream"):
                    return httpx.Response(
                        200,
                        content=(FIXTURES / (fixture + ".sse")).read_bytes(),
                        headers={"content-type": "text/event-stream"},
                    )
                return httpx.Response(
                    200, json=json.loads((FIXTURES / (fixture + ".json")).read_text())
                )

            mock.post("http://provider.test/v1/" + endpoint).mock(side_effect=reply)
        chat = await oa.chat.completions.create(
            model="gpt-mock", max_tokens=1024, messages=[{"role": "user", "content": "hello"}]
        )
        assert chat.choices[0].message.content == "mock reply"
        stream = await oa.chat.completions.create(
            model="gpt-mock",
            max_tokens=1024,
            stream=True,
            messages=[{"role": "user", "content": "hello"}],
        )
        assert (
            "".join(
                [chunk.choices[0].delta.content or "" async for chunk in stream if chunk.choices]
            )
            == "mock reply"
        )
        response = await oa.responses.create(
            model="gpt-mock", input="hello", max_output_tokens=1024
        )
        assert response.output_text == "mock reply"
        stream = await oa.responses.create(
            model="gpt-mock", input="hello", max_output_tokens=1024, stream=True
        )
        assert any(
            event.type == "response.completed" for event in [event async for event in stream]
        )
        embedding = await oa.embeddings.create(
            model="text-embedding-mock", input="hello", encoding_format="float"
        )
        assert embedding.data[0].embedding == [0.1, 0.2, 0.3]
        message = await an.messages.create(
            model="claude-mock", max_tokens=1024, messages=[{"role": "user", "content": "hello"}]
        )
        assert message.content[0].text == "mock reply"
        async with an.messages.stream(
            model="claude-mock", max_tokens=1024, messages=[{"role": "user", "content": "hello"}]
        ) as stream:
            assert "".join([text async for text in stream.text_stream]) == "mock reply"


@pytest.mark.asyncio
async def test_tool_calls_long_output_and_tenant_isolation(governed):
    names = ["chat", "responses", "embeddings", "messages", "gemini"]
    for (path, provider, model, extra), name in zip(ROUTES, names, strict=True):
        fixture = json.loads((FIXTURES / (name + ".json")).read_text())
        tool = copy.deepcopy(fixture)
        call = {"name": "weather", "arguments": '{"city":"Almaty"}'}
        if name == "chat":
            tool["choices"][0]["message"] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": call}],
            }
        elif name == "responses":
            tool["output"] = [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "id": "fc_1",
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "status": "completed",
                }
            ]
        elif name == "messages":
            tool["content"] = [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "weather",
                    "input": {"city": "Almaty"},
                }
            ]
        elif name == "gemini":
            tool["candidates"][0]["content"]["parts"] = [
                {"functionCall": {"name": "weather", "args": {"city": "Almaty"}}}
            ]
        # Arbitrary long native output must pass through without translation or truncation.
        long_output = {**fixture, "mock_long_output": "x" * 64000}
        for payload in (tool, long_output):
            raw = json.dumps(payload, separators=(",", ":")).encode()
            with respx.mock(assert_all_called=True) as mock:
                route = mock.post("http://provider.test" + path).respond(200, content=raw)
                for tenant in (governed.a, governed.b):
                    response = await governed.http.post(
                        path, json={"model": model, **extra}, headers=headers(provider, tenant.key)
                    )
                    assert response.content == raw and response.status_code == 200
                assert [
                    r.request.headers.get("authorization")
                    or r.request.headers.get("x-api-key")
                    or r.request.headers.get("x-goog-api-key")
                    for r in route.calls
                ] == [
                    "Bearer provider-A" if provider == "openai" else "provider-A",
                    "Bearer provider-B" if provider == "openai" else "provider-B",
                ]
    async with governed.factory() as session:
        events = (await session.execute(select(Event))).scalars().all()
        assert {event.client_id for event in events} == {governed.a.client.id, governed.b.client.id}
        assert len(events) == 20


@pytest.mark.asyncio
async def test_interrupted_streams_settle_estimates(governed):
    cases = [
        ("/v1/chat/completions", "openai", "gpt-mock", {"max_tokens": 16}, "chat"),
        ("/v1/responses", "openai", "gpt-mock", {"max_output_tokens": 16}, "responses"),
        ("/v1/messages", "anthropic", "claude-mock", {"max_tokens": 16}, "messages"),
        (
            "/v1beta/models/gemini-mock:streamGenerateContent?alt=sse",
            "gemini",
            "gemini-mock",
            {"generationConfig": {"maxOutputTokens": 16}},
            "gemini",
        ),
    ]
    for path, provider, model, extra, name in cases:
        raw = (FIXTURES / (name + ".sse")).read_bytes().split(b"\n\n")[0] + b"\n\n"
        if name == "gemini":
            raw = b'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}\n\n'
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://provider.test" + path).respond(
                200, content=raw, headers={"content-type": "text/event-stream"}
            )
            response = await governed.http.post(
                path,
                json={"model": model, "stream": True, **extra},
                headers=headers(provider, governed.a.key),
            )
            assert response.content == raw
    async with governed.factory() as session:
        events = (await session.execute(select(Event))).scalars().all()
        assert len(events) == 4 and all(event.estimated for event in events)
        budgets = (await session.execute(select(Budget))).scalars().all()
        assert all(budget.reserved_micro == 0 for budget in budgets)
