import os
from datetime import UTC, datetime
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database import get_session
from core.models import Base, Client, VirtualKey, Workflow
from core.security import virtual_key_hash
from gateway.auth import AuthContext, authenticate_openai
from gateway.errors import GatewayError, openai_error_handler


@pytest.mark.asyncio
async def test_virtual_key_resolves_tenant_and_revocation() -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the auth integration test")

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    key = "gk_" + "x" * 32
    async with factory() as session:
        client = Client(name="Client A")
        session.add(client)
        await session.flush()
        workflow = Workflow(client_id=client.id, name="invoices", risk_class="R2", policy={})
        session.add(workflow)
        await session.flush()
        record = VirtualKey(workflow_id=workflow.id, key_hash=virtual_key_hash(key), prefix=key[:8])
        session.add(record)
        await session.commit()
        key_id = record.id

    probe = FastAPI()
    probe.add_exception_handler(GatewayError, openai_error_handler)

    @probe.get("/probe")
    async def auth_probe(
        auth: Annotated[AuthContext, Depends(authenticate_openai)],
    ) -> dict[str, str]:
        return {"client_id": str(auth.client_id), "workflow_id": str(auth.workflow_id)}

    async def test_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    probe.dependency_overrides[get_session] = test_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=probe), base_url="http://gateway.test"
        ) as http:
            for wrong in ({}, {"Authorization": "Bearer gk_wrong"}):
                response = await http.get("/probe", headers=wrong)
                assert response.status_code == 401
                assert response.json()["error"]["code"] == "invalid_api_key"
            valid = await http.get("/probe", headers={"Authorization": f"Bearer {key}"})
            assert valid.status_code == 200
            assert valid.json() == {"client_id": str(client.id), "workflow_id": str(workflow.id)}

            async with factory() as session:
                stored = await session.get(VirtualKey, key_id)
                assert stored is not None
                stored.revoked_at = datetime.now(UTC)
                await session.commit()
            revoked = await http.get("/probe", headers={"Authorization": f"Bearer {key}"})
            assert revoked.status_code == 401
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
