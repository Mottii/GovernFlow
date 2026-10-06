import base64
import os
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database import get_session
from core.models import Base, Budget, Client, Price, ProviderCredential, VirtualKey, Workflow
from core.security import encrypt_provider_key, virtual_key_hash
from gateway.app import app


@pytest_asyncio.fixture
async def governed(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("REPLAY_QUEUE_PATH", str(tmp_path / "replay.sqlite3"))
    from gateway.outage import _cache

    _cache.clear()
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", base64.b64encode(b"t" * 32).decode())
    monkeypatch.setenv("KILL_CACHE_TTL_S", "0")
    for provider in ("OPENAI", "ANTHROPIC", "GEMINI"):
        monkeypatch.setenv(provider + "_BASE_URL", "http://provider.test")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    tenants = []
    async with factory() as session:
        for name in ("A", "B"):
            client = Client(name=name)
            session.add(client)
            await session.flush()
            workflow = Workflow(
                client_id=client.id, name="test", risk_class="R1", policy={"models": ["*"]}
            )
            session.add(workflow)
            await session.flush()
            key = "gk_" + name * 32
            session.add(
                VirtualKey(workflow_id=workflow.id, key_hash=virtual_key_hash(key), prefix=key[:8])
            )
            session.add(
                Budget(
                    workflow_id=workflow.id,
                    period=datetime.now(UTC).strftime("%Y-%m"),
                    cap_micro=100000,
                )
            )
            for provider in ("openai", "anthropic", "gemini"):
                encrypted, nonce = encrypt_provider_key(
                    "provider-" + name, client.id, provider, "default"
                )
                session.add(
                    ProviderCredential(
                        client_id=client.id,
                        provider=provider,
                        label="default",
                        ciphertext=encrypted,
                        nonce=nonce,
                    )
                )
            tenants.append(SimpleNamespace(client=client, workflow=workflow, key=key))
        for provider, model in (
            ("openai", "gpt-mock"),
            ("openai", "text-embedding-mock"),
            ("anthropic", "claude-mock"),
            ("gemini", "gemini-mock"),
        ):
            session.add(
                Price(
                    provider=provider,
                    model=model,
                    effective_from=date(2020, 1, 1),
                    input=1000000,
                    output=1000000,
                    cache_read=1000000,
                    cache_write=1000000,
                    reasoning=1000000,
                )
            )
        await session.commit()

    async def test_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as http:
            yield SimpleNamespace(http=http, factory=factory, a=tenants[0], b=tenants[1])
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
