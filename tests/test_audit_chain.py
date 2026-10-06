import os

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from audit.chain import AuditEventInput, append_event, verify_chain
from core.models import Base, Client, Event


@pytest.mark.asyncio
async def test_chain_detects_tampering_after_many_events() -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the audit chain test")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            client = Client(name="Audited client")
            session.add(client)
            await session.flush()
            client_id = client.id
            for index in range(1000):
                await append_event(
                    session,
                    AuditEventInput(
                        client_id=client_id,
                        type="provider_call" if index % 2 else "approval_decision",
                        cost_micro=index,
                    ),
                )
            await session.commit()
        async with factory() as session:
            assert await verify_chain(session, client_id) == (True, None)
        async with factory() as session:
            await session.execute(update(Event).where(Event.id == 500).values(type="altered"))
            await session.commit()
        async with factory() as session:
            assert await verify_chain(session, client_id) == (False, 500)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
