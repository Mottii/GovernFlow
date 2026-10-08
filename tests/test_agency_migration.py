"""Upgrade acceptance against a populated 0014 installation."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_agency_backfill_preserves_identifiers_credentials_and_grants():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    env = {**os.environ, "DATABASE_URL": url}
    command = [sys.executable, "-m", "alembic"]
    subprocess.run([*command, "upgrade", "0014"], env=env, check=True, capture_output=True)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            client = (
                await conn.execute(
                    text("INSERT INTO clients (name) VALUES ('Legacy') RETURNING id")
                )
            ).scalar_one()
            workflow = (
                await conn.execute(
                    text(
                        "INSERT INTO workflows (client_id, name, risk_class) "
                        "VALUES (:client, 'Legacy', 'R1') RETURNING id"
                    ),
                    {"client": client},
                )
            ).scalar_one()
            key_id = (
                await conn.execute(
                    text(
                        "INSERT INTO virtual_keys (workflow_id, key_hash, prefix) "
                        "VALUES (:workflow, decode('abcd', 'hex'), 'gk_test') RETURNING id"
                    ),
                    {"workflow": workflow},
                )
            ).scalar_one()
            for role in ("admin", "operator", "approver", "viewer"):
                await conn.execute(
                    text(
                        "INSERT INTO users (email, role, client_id, password_hash, password_salt) "
                        "VALUES (:email, :role, :client, decode('aabb', 'hex'), "
                        "decode('ccdd', 'hex'))"
                    ),
                    {
                        "email": role + "@legacy.test",
                        "role": role,
                        "client": None if role == "admin" else client,
                    },
                )
            await conn.execute(
                text(
                    "INSERT INTO admin_sessions (token_hash, user_id, expires_at) "
                    "SELECT decode('1234', 'hex'), id, now() + interval '1 hour' "
                    "FROM users WHERE role = 'viewer'"
                )
            )
            original = (
                await conn.execute(
                    text(
                        "SELECT id, email, role, client_id, "
                        "password_hash, password_salt, active "
                        "FROM users ORDER BY email"
                    )
                )
            ).all()
        subprocess.run([*command, "upgrade", "head"], env=env, check=True, capture_output=True)
        async with engine.connect() as conn:
            assert (
                original
                == (
                    await conn.execute(
                        text(
                            "SELECT id, email, role, client_id, "
                            "password_hash, password_salt, active "
                            "FROM users ORDER BY email"
                        )
                    )
                ).all()
            )
            assert (await conn.execute(text("SELECT id FROM clients"))).scalar_one() == client
            assert (await conn.execute(text("SELECT id FROM workflows"))).scalar_one() == workflow
            assert (await conn.execute(text("SELECT id, key_hash FROM virtual_keys"))).one() == (
                key_id,
                bytes.fromhex("abcd"),
            )
            memberships = (
                await conn.execute(
                    text(
                        "SELECT u.role, m.role, m.all_clients, m.active, a.client_id "
                        "FROM users u JOIN agency_memberships m ON m.user_id = u.id "
                        "LEFT JOIN client_assignments a ON a.user_id = u.id ORDER BY u.role"
                    )
                )
            ).all()
            assert memberships == [
                (role, role, role == "admin", True, None if role == "admin" else client)
                for role in ("admin", "approver", "operator", "viewer")
            ]
            assert (
                str((await conn.execute(text("SELECT agency_id FROM admin_sessions"))).scalar_one())
                == "00000000-0000-0000-0000-000000000001"
            )
        # A downgrade must refuse to broaden real multi-agency grants into legacy global access.
        async with engine.begin() as conn:
            other = (
                await conn.execute(text("INSERT INTO agencies (name) VALUES ('New') RETURNING id"))
            ).scalar_one()
        result = subprocess.run([*command, "downgrade", "0014"], env=env, capture_output=True)
        assert (
            result.returncode != 0
            and b"Cannot downgrade a multi-agency installation" in result.stderr
        )
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM agencies WHERE id = :other"), {"other": other})
    finally:
        await engine.dispose()
        subprocess.run([*command, "downgrade", "base"], env=env, check=True, capture_output=True)
