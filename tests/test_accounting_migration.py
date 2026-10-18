"""Maintenance-window upgrade preserves balances and conservatively adopts pending calls."""

import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database import set_agency_context
from core.models import DEFAULT_AGENCY_ID, Budget, ProviderCall
from policy.accounting import Evidence, apply
from policy.reap import reap_stale


async def test_populated_0018_upgrade_preserves_then_corrects_uncertain_calls():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    env = {**os.environ, "DATABASE_URL": url}
    command = [sys.executable, "-m", "alembic"]
    subprocess.run([*command, "upgrade", "0018"], env=env, check=True, capture_output=True)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            client = (
                await connection.execute(
                    text("INSERT INTO clients(name) VALUES('Legacy accounting') RETURNING id")
                )
            ).scalar_one()
            workflow = (
                await connection.execute(
                    text(
                        "INSERT INTO workflows(client_id,name,risk_class) "
                        "VALUES(:client,'Legacy','R1') RETURNING id"
                    ),
                    {"client": client},
                )
            ).scalar_one()
            budget = (
                await connection.execute(
                    text(
                        "INSERT INTO budgets(workflow_id,period,cap_micro,reserved_micro) "
                        "VALUES(:workflow,'2026-09',1000,80) RETURNING id"
                    ),
                    {"workflow": workflow},
                )
            ).scalar_one()
            run = (
                await connection.execute(
                    text(
                        "INSERT INTO runs(workflow_id,external_run_id,reserved_micro) "
                        "VALUES(:workflow,'legacy',80) RETURNING id"
                    ),
                    {"workflow": workflow},
                )
            ).scalar_one()
            reservation = (
                await connection.execute(
                    text(
                        "INSERT INTO reservations(budget_id,run_id,est_micro) "
                        "VALUES(:budget,:run,80) RETURNING id"
                    ),
                    {"budget": budget, "run": run},
                )
            ).scalar_one()
        subprocess.run([*command, "upgrade", "head"], env=env, check=True, capture_output=True)
        async with factory() as session:
            await set_agency_context(session, DEFAULT_AGENCY_ID)
            call = await session.get(ProviderCall, reservation)
            assert (
                call.state == "dispatched" and call.uncertainty_reason == "legacy_dispatch_unknown"
            )
            original = await session.get(Budget, budget)
            assert (original.spent_micro, original.reserved_micro) == (0, 80)
            assert await reap_stale(session, datetime.now(UTC)) == 1
            await session.refresh(original)
            assert (original.spent_micro, original.reserved_micro) == (80, 0)
            import uuid

            await apply(
                session,
                Evidence(
                    evidence_id=uuid.uuid4(),
                    agency_id=DEFAULT_AGENCY_ID,
                    client_id=client,
                    call_id=reservation,
                    kind="measured",
                    amount_micro=15,
                    reason="late_usage",
                ),
            )
            await session.commit()
            await session.refresh(original)
            assert original.spent_micro == 15 and original.period == "2026-09"
        result = subprocess.run([*command, "downgrade", "0018"], env=env, capture_output=True)
        assert (
            result.returncode != 0
            and b"Cannot downgrade active provider accounting" in result.stderr
        )
    finally:
        # This disposable database deliberately has protected financial history.
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            await connection.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
        await engine.dispose()
