import base64
import json
import os
from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from audit.chain import verify_chain
from core.database import get_session
from core.models import (
    Base,
    Budget,
    Client,
    Event,
    KillFlag,
    Price,
    ProviderCredential,
    VirtualKey,
    Workflow,
)
from core.security import encrypt_provider_key, virtual_key_hash
from gateway.adapters.openai import chat_usage
from gateway.app import app


@pytest.mark.asyncio
async def test_chat_proxy_preserves_body_and_uses_provider_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the chat proxy test")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", base64.b64encode(b"a" * 32).decode())
    monkeypatch.setenv("OPENAI_BASE_URL", "http://mock-provider:8002")
    monkeypatch.setenv("DEFAULT_MAX_OUTPUT_TOKENS", "16")
    monkeypatch.setenv("KILL_CACHE_TTL_S", "0")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    key = "gk_" + "z" * 32
    async with factory() as session:
        client = Client(name="Acme")
        session.add(client)
        await session.flush()
        workflow = Workflow(client_id=client.id, name="chat", risk_class="R0", policy={})
        session.add(workflow)
        await session.flush()
        session.add(
            VirtualKey(workflow_id=workflow.id, key_hash=virtual_key_hash(key), prefix=key[:8])
        )
        ciphertext, nonce = encrypt_provider_key(
            "real-provider-key", client.id, "openai", "default"
        )
        session.add(
            ProviderCredential(
                client_id=client.id,
                provider="openai",
                label="default",
                ciphertext=ciphertext,
                nonce=nonce,
            )
        )
        session.add(
            Budget(
                workflow_id=workflow.id, period=datetime.now(UTC).strftime("%Y-%m"), cap_micro=50
            )
        )
        session.add(
            Price(
                provider="openai",
                model="gpt-mock",
                effective_from=date(2020, 1, 1),
                input=1_000_000,
                output=1_000_000,
                cache_read=1_000_000,
                cache_write=1_000_000,
                reasoning=1_000_000,
            )
        )
        session.add(KillFlag(scope="global", scope_id="", active=True, reason="emergency"))
        await session.commit()

    async def test_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    request_bytes = b'{"model":"gpt-mock","messages":[{"role":"user","content":"hello"}]}'
    response_bytes = (
        b'{"id":"chatcmpl_mock","usage":{"prompt_tokens":12,"completion_tokens":3},"choices":[]}'
    )
    stream_head = b'data: {"choices":[{"delta":{"content":"hello"}}],"usage":null}\n\n'
    stream_usage = b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    stream_done = b"data: [DONE]\n\n"

    def provider_reply(request: httpx.Request) -> httpx.Response:
        upstream_body = json.loads(request.content)
        if upstream_body.get("stream"):
            chunks = stream_head + stream_done
            if upstream_body["messages"][0]["content"] != "cut":
                chunks = stream_head + stream_usage + stream_done
            return httpx.Response(
                200, content=chunks, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, content=response_bytes)

    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("http://mock-provider:8002/v1/chat/completions").mock(
                side_effect=provider_reply
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
            ) as http:
                killed = await http.post(
                    "/v1/chat/completions",
                    content=request_bytes,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                async with factory() as session:
                    await session.execute(
                        update(KillFlag)
                        .where(KillFlag.scope == "global")
                        .values(active=False, reason=None)
                    )
                    await session.commit()
                for scope, scope_id in (
                    ("client", str(client.id)),
                    ("workflow", str(workflow.id)),
                ):
                    async with factory() as session:
                        session.add(
                            KillFlag(scope=scope, scope_id=scope_id, active=True, reason="paused")
                        )
                        await session.commit()
                    scope_killed = await http.post(
                        "/v1/chat/completions",
                        content=request_bytes,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                    )
                    assert scope_killed.status_code == 503
                    async with factory() as session:
                        await session.execute(
                            update(KillFlag)
                            .where(KillFlag.scope == scope, KillFlag.scope_id == scope_id)
                            .values(active=False, reason=None)
                        )
                        await session.commit()
                models_response = await http.get(
                    "/v1/models", headers={"Authorization": f"Bearer {key}"}
                )
                response = await http.post(
                    "/v1/chat/completions",
                    content=request_bytes,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                denied = await http.post(
                    "/v1/chat/completions",
                    content=request_bytes,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                async with factory() as session:
                    budget = (await session.execute(select(Budget))).scalar_one()
                    budget.cap_micro = 1000
                    await session.commit()
                stream_body = (
                    b'{"model":"gpt-mock","stream":true,'
                    b'"messages":[{"role":"user","content":"hello"}]}'
                )
                streamed = await http.post(
                    "/v1/chat/completions",
                    content=stream_body,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                cut_body = (
                    b'{"model":"gpt-mock","stream":true,'
                    b'"messages":[{"role":"user","content":"cut"}]}'
                )
                cut = await http.post(
                    "/v1/chat/completions",
                    content=cut_body,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
            assert route.called
            assert models_response.status_code == 200
            assert models_response.json()["data"][0]["id"] == "gpt-mock"
            assert killed.status_code == 503
            assert killed.json()["error"]["code"] == "workflow_killed"
            assert route.calls[0].request.content == request_bytes
            assert route.calls[0].request.headers["Authorization"] == "Bearer real-provider-key"
            assert response.status_code == 200
            assert response.content == response_bytes
            assert denied.status_code == 429
            assert denied.json()["error"]["code"] == "budget_exceeded"
            assert streamed.status_code == 200
            assert streamed.content == stream_head + stream_done
            assert json.loads(route.calls[1].request.content)["stream_options"] == {
                "include_usage": True
            }
            assert cut.status_code == 200
            assert cut.content == stream_head + stream_done
            async with factory() as session:
                budget = (await session.execute(select(Budget))).scalar_one()
                assert budget.spent_micro > 22
                assert budget.reserved_micro == 0
                events = (await session.execute(select(Event).order_by(Event.id))).scalars().all()
                assert len(events) == 3
                assert events[0].cost_micro == 15
                assert events[1].cost_micro == 7
                assert events[1].estimated is False
                assert events[2].estimated is True
                assert await verify_chain(session, client.id) == (True, None)
            usage = chat_usage(json.loads(response_bytes))
            assert usage is not None
            assert (usage.input, usage.output) == (12, 3)
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
