import base64
import json
import os

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database import get_session
from core.models import Base, Client, ProviderCredential, VirtualKey, Workflow
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
        await session.commit()

    async def test_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    request_bytes = b'{"model":"gpt-mock","messages":[{"role":"user","content":"hello"}]}'
    response_bytes = (
        b'{"id":"chatcmpl_mock","usage":{"prompt_tokens":12,"completion_tokens":3},"choices":[]}'
    )
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post("http://mock-provider:8002/v1/chat/completions").mock(
                return_value=httpx.Response(200, content=response_bytes)
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
            ) as http:
                response = await http.post(
                    "/v1/chat/completions",
                    content=request_bytes,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
            assert route.called
            assert route.calls[0].request.content == request_bytes
            assert route.calls[0].request.headers["Authorization"] == "Bearer real-provider-key"
            assert response.status_code == 200
            assert response.content == response_bytes
            usage = chat_usage(json.loads(response_bytes))
            assert usage is not None
            assert (usage.input, usage.output) == (12, 3)
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
