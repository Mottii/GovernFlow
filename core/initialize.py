"""Operator-only schema initialization and restricted application-login provisioning."""

import asyncio
import os
import re
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.database import make_engine
from core.pricing import seed


async def initialize() -> None:
    if os.environ.get("MAINTENANCE_MODE", "").lower() != "true":
        raise RuntimeError("Initialization requires an explicit maintenance process")
    password = os.environ.get("APP_DATABASE_PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", password):
        raise RuntimeError(
            "APP_DATABASE_PASSWORD must be URL-safe and contain at least 20 characters"
        )
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    engine = make_engine()
    try:
        async with engine.begin() as connection:
            exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname='governflow_app'")
                )
            ).scalar_one_or_none()
            if not exists:
                await connection.execute(text("CREATE ROLE governflow_app LOGIN"))
            statement: str = (
                await connection.execute(
                    text(
                        "SELECT format('ALTER ROLE governflow_app WITH LOGIN "
                        "NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', "
                        "CAST(:password AS text))"
                    ),
                    {"password": password},
                )
            ).scalar_one()
            try:
                await connection.execute(text(statement))
            except DBAPIError:
                # Do not expose a rendered password-bearing DDL statement in exception logs.
                raise RuntimeError(
                    "Could not provision runtime login; check operator permissions"
                ) from None
            await connection.execute(text("GRANT governflow_runtime TO governflow_app"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await seed(session)
            from core.rotation import observe, preflight

            await preflight(session)
            await observe(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(initialize())
