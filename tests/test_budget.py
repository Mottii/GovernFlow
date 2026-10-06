import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.models import Base, Budget, Client, Workflow
from policy.budget import HeldReservation, reserve, settle
from policy.reap import reap_stale


@pytest.mark.asyncio
async def test_parallel_reservations_never_cross_cap() -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the budget race test")
    engine = create_async_engine(url, pool_size=20, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            client = Client(name="Race test")
            session.add(client)
            await session.flush()
            workflow = Workflow(client_id=client.id, name="race", risk_class="R0", policy={})
            session.add(workflow)
            await session.flush()
            workflow_id = workflow.id
            session.add(
                Budget(
                    workflow_id=workflow_id,
                    period=datetime.now(UTC).strftime("%Y-%m"),
                    cap_micro=50,
                )
            )
            await session.commit()

        async def one_call(index: int) -> HeldReservation | None:
            async with factory() as session:
                return await reserve(session, workflow_id, f"race-{index}", 1, 100)

        results = await asyncio.gather(*(one_call(index) for index in range(200)))
        accepted = [result for result in results if result is not None]
        assert len(accepted) == 50
        async with factory() as session:
            budget = (await session.execute(select(Budget))).scalar_one()
            assert budget.spent_micro + budget.reserved_micro == 50

        for held in accepted:
            async with factory() as session:
                assert await settle(session, held, 1)
        async with factory() as session:
            budget = (await session.execute(select(Budget))).scalar_one()
            assert budget.spent_micro == 50
            assert budget.reserved_micro == 0
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_overlapping_reservations_and_settlements_do_not_deadlock() -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the budget race test")
    engine = create_async_engine(url, pool_size=20, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            client = Client(name="Mixed race test")
            session.add(client)
            await session.flush()
            workflow = Workflow(client_id=client.id, name="mixed race", risk_class="R0", policy={})
            session.add(workflow)
            await session.flush()
            workflow_id = workflow.id
            session.add(
                Budget(
                    workflow_id=workflow_id,
                    period=datetime.now(UTC).strftime("%Y-%m"),
                    cap_micro=100,
                )
            )
            await session.commit()

        async def one_call(index: int) -> None:
            async with factory() as session:
                held = await reserve(session, workflow_id, "shared-run", 1, 100)
                assert held is not None
                await asyncio.sleep(0)
                assert await settle(session, held, 1)

        async with asyncio.timeout(15):
            await asyncio.gather(*(one_call(index) for index in range(50)))
        async with factory() as session:
            budget = (await session.execute(select(Budget))).scalar_one()
            assert (budget.spent_micro, budget.reserved_micro) == (50, 0)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_reservation_released_once() -> None:
    url = os.environ.get("APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL is required for the budget test")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            client = Client(name="Reaper test")
            session.add(client)
            await session.flush()
            workflow = Workflow(client_id=client.id, name="reaper", risk_class="R0", policy={})
            session.add(workflow)
            await session.flush()
            workflow_id = workflow.id
            session.add(
                Budget(
                    workflow_id=workflow_id,
                    period=datetime.now(UTC).strftime("%Y-%m"),
                    cap_micro=100,
                )
            )
            await session.commit()
            assert await reserve(session, workflow_id, "stale", 40, 100)
            assert await reap_stale(session, datetime.now(UTC) + timedelta(hours=1)) == 1
            assert await reap_stale(session, datetime.now(UTC) + timedelta(hours=1)) == 0
            budget = (await session.execute(select(Budget))).scalar_one()
            assert (budget.spent_micro, budget.reserved_micro) == (0, 0)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
