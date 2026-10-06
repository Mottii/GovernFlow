import base64
import os

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from admin_api.app import app
from core.database import get_session
from core.models import Base, ProviderCredential, VirtualKey
from core.security import decrypt_provider_key, virtual_key_hash


@pytest.mark.asyncio
async def test_admin_creates_scoped_records_without_storing_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the admin integration test")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", base64.b64encode(b"a" * 32).decode())

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def test_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    headers = {"Authorization": "Bearer test-admin-token"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://admin.test"
        ) as client:
            assert (await client.post("/admin/clients", json={"name": "Acme"})).status_code == 401
            created_client = await client.post(
                "/admin/clients", json={"name": "Acme"}, headers=headers
            )
            assert created_client.status_code == 201
            client_id = created_client.json()["id"]

            created_workflow = await client.post(
                "/admin/workflows",
                json={"client_id": client_id, "name": "invoices", "risk_class": "R2"},
                headers=headers,
            )
            assert created_workflow.status_code == 201
            workflow_id = created_workflow.json()["id"]

            budget = await client.post(
                "/admin/budgets",
                json={"workflow_id": workflow_id, "period": "2026-10", "cap_micro": 40_000_000},
                headers=headers,
            )
            assert budget.status_code == 201

            credential = await client.post(
                "/admin/provider-credentials",
                json={"client_id": client_id, "provider": "openai", "key": "upstream-secret"},
                headers=headers,
            )
            assert credential.status_code == 201
            assert "upstream-secret" not in credential.text

            created_key = await client.post(
                "/admin/keys", json={"workflow_id": workflow_id}, headers=headers
            )
            assert created_key.status_code == 201
            plaintext = created_key.json()["key"]
            assert plaintext.startswith("gk_")

            async with factory() as session:
                stored_key = (await session.execute(select(VirtualKey))).scalar_one()
                stored_credential = (await session.execute(select(ProviderCredential))).scalar_one()
                assert stored_key.key_hash == virtual_key_hash(plaintext)
                assert plaintext.encode() not in stored_key.key_hash
                assert b"upstream-secret" not in stored_credential.ciphertext
                assert (
                    decrypt_provider_key(
                        stored_credential.ciphertext,
                        stored_credential.nonce,
                        stored_credential.client_id,
                        stored_credential.provider,
                        stored_credential.label,
                    )
                    == "upstream-secret"
                )

            revoked = await client.delete(
                f"/admin/keys/{created_key.json()['id']}", headers=headers
            )
            assert revoked.status_code == 204
            killed = await client.post(
                "/admin/kill",
                json={"scope": "workflow", "scope_id": workflow_id, "active": True},
                headers=headers,
            )
            assert killed.status_code == 200
            assert killed.json()["active"] is True
            async with factory() as session:
                assert (
                    await session.execute(select(VirtualKey))
                ).scalar_one().revoked_at is not None
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
