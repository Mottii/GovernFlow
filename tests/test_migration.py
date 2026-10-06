import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_initial_migration_round_trip_and_immutable_events() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the Postgres migration test")

    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO events (client_id, type, hash) "
                    "VALUES (gen_random_uuid(), 'migration_test', decode(repeat('00', 32), 'hex'))"
                )
            )
        with pytest.raises(DBAPIError, match="events is append-only"):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE events SET type = 'tampered' WHERE type = 'migration_test'")
                )
    finally:
        await engine.dispose()
        subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], env=env, check=True)
