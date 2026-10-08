"""Acceptance on a migrated database using an actual unprivileged application login."""

import base64
import os
import secrets
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from admin_api.app import app as admin_app
from admin_api.auth import password_hash, token_hash
from core.database import get_session, set_agency_context
from core.models import (
    DEFAULT_AGENCY_ID,
    AdminSession,
    Agency,
    AgencyMembership,
    Alert,
    Approval,
    Budget,
    Client,
    ClientAssignment,
    Event,
    KillFlag,
    MonthlyMetric,
    Notification,
    Price,
    ProviderAccount,
    ProviderCredential,
    RateBucket,
    ReconcileJob,
    ReconcileSnapshot,
    ReplayReceipt,
    Reservation,
    Run,
    User,
    VirtualKey,
    WebhookReceipt,
    Workflow,
)
from core.security import encrypt_provider_key, virtual_key_hash
from gateway.app import app as gateway_app

PASSWORD = "rls fixture password"
TENANT_TABLES = (
    Client,
    Workflow,
    VirtualKey,
    ProviderCredential,
    Budget,
    Run,
    Reservation,
    Event,
    Alert,
    Approval,
    RateBucket,
    Notification,
    ProviderAccount,
    ReconcileSnapshot,
    ReconcileJob,
    MonthlyMetric,
    AgencyMembership,
    ClientAssignment,
    AdminSession,
    ReplayReceipt,
    WebhookReceipt,
)


@pytest.fixture
async def isolated_database(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    monkeypatch.setenv("ENFORCE_AGENCY_RLS", "true")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", base64.b64encode(b"t" * 32).decode())
    monkeypatch.setenv("DECISION_SIGNING_KEY", base64.b64encode(b"d" * 32).decode())
    monkeypatch.setenv("OPENAI_BASE_URL", "http://provider.test")
    monkeypatch.setenv("KILL_CACHE_TTL_S", "0")
    env = {**os.environ, "DATABASE_URL": url}
    command = [sys.executable, "-m", "alembic"]
    subprocess.run([*command, "upgrade", "head"], env=env, check=True, capture_output=True)
    owner_engine = create_async_engine(url)
    owner = async_sessionmaker(owner_engine, expire_on_commit=False)
    runtime_engine = None
    role = "governflow_test_" + secrets.token_hex(5)
    password = secrets.token_urlsafe(32)
    try:
        async with owner_engine.begin() as conn:
            # Generated role/password contain only safe SQL identifier/string characters.
            await conn.execute(
                text(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            )
            await conn.execute(text(f"GRANT governflow_runtime TO {role}"))
        tenants = []
        salt = b"fixture salt 1234"
        async with owner() as session:
            agency_b = Agency(name="Agency B")
            session.add(agency_b)
            await session.flush()
            for agency_id, prefix in ((DEFAULT_AGENCY_ID, "A"), (agency_b.id, "B")):
                for index in (1, 2):
                    client = Client(agency_id=agency_id, name=f"{prefix}{index}")
                    session.add(client)
                    await session.flush()
                    workflow = Workflow(
                        client_id=client.id,
                        name=client.name,
                        risk_class="R2",
                        policy={"models": ["*"], "actions": {"publish": {}}},
                    )
                    session.add(workflow)
                    await session.flush()
                    key = "gk_" + secrets.token_urlsafe(32)
                    budget = Budget(
                        workflow_id=workflow.id,
                        period=datetime.now(UTC).strftime("%Y-%m"),
                        cap_micro=1000000,
                        reserved_micro=10,
                    )
                    run = Run(workflow_id=workflow.id, external_run_id="fixture", reserved_micro=10)
                    encrypted, nonce = encrypt_provider_key(
                        "mock-only", client.id, "openai", "default"
                    )
                    session.add_all(
                        [
                            VirtualKey(
                                workflow_id=workflow.id,
                                key_hash=virtual_key_hash(key),
                                prefix=key[:8],
                            ),
                            ProviderCredential(
                                client_id=client.id,
                                provider="openai",
                                label="default",
                                ciphertext=encrypted,
                                nonce=nonce,
                            ),
                            budget,
                            run,
                        ]
                    )
                    await session.flush()
                    approval = Approval(
                        workflow_id=workflow.id,
                        action_name="publish",
                        action_hash=b"f" * 32,
                        action_summary="Fixture",
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                    user = User(
                        email=f"{prefix}{index}@fixture.test".casefold(),
                        role="viewer",
                        password_hash=password_hash(PASSWORD, salt),
                        password_salt=salt,
                    )
                    session.add_all([approval, user])
                    await session.flush()
                    membership = AgencyMembership(
                        agency_id=agency_id, user_id=user.id, role="viewer"
                    )
                    session.add(membership)
                    await session.flush()
                    session.add_all(
                        [
                            ClientAssignment(
                                agency_id=agency_id, user_id=user.id, client_id=client.id
                            ),
                            AdminSession(
                                token_hash=token_hash(f"session-{prefix}{index}"),
                                user_id=user.id,
                                agency_id=agency_id,
                                expires_at=datetime.now(UTC) + timedelta(hours=1),
                            ),
                            Reservation(budget_id=budget.id, run_id=run.id, est_micro=10),
                            Event(
                                client_id=client.id, run_id=run.id, type="fixture", hash=b"f" * 32
                            ),
                            Alert(client_id=client.id, workflow_id=workflow.id, kind="fixture"),
                            Notification(approval_id=approval.id, channel="email"),
                            RateBucket(workflow_id=workflow.id, minute=datetime.now(UTC), count=1),
                            ProviderAccount(
                                client_id=client.id, provider="openai", external_id=client.name
                            ),
                            ReconcileJob(
                                client_id=client.id,
                                provider="openai",
                                day=date.today(),
                                status="complete",
                            ),
                            ReconcileSnapshot(
                                client_id=client.id,
                                provider="openai",
                                day=date.today(),
                                kind="cost",
                                dimension="total",
                                gateway={},
                                observed={},
                            ),
                            MonthlyMetric(
                                client_id=client.id,
                                period="2026-10",
                                minutes_saved=10,
                                basis="measured",
                                updated_by="fixture",
                            ),
                            ReplayReceipt(id=uuid.uuid4(), agency_id=agency_id),
                            WebhookReceipt(id=uuid.uuid4(), agency_id=agency_id),
                            KillFlag(scope="client", scope_id=str(client.id), active=False),
                            KillFlag(scope="workflow", scope_id=str(workflow.id), active=False),
                        ]
                    )
                    tenants.append(
                        SimpleNamespace(
                            agency_id=agency_id,
                            client=client,
                            workflow=workflow,
                            key=key,
                            budget=budget,
                            run=run,
                            approval=approval,
                            user=user,
                        )
                    )
            session.add_all(
                [
                    Price(
                        provider="openai",
                        model="gpt-mock",
                        effective_from=date(2020, 1, 1),
                        input=1000000,
                        output=1000000,
                    ),
                    KillFlag(scope="global", scope_id="", active=False),
                ]
            )
            await session.commit()
        runtime_url = make_url(url).set(username=role, password=password)
        runtime_engine = create_async_engine(runtime_url, pool_size=1, max_overflow=0)
        runtime = async_sessionmaker(runtime_engine, expire_on_commit=False)

        async def runtime_session():
            async with runtime() as session:
                yield session

        admin_app.dependency_overrides[get_session] = runtime_session
        gateway_app.dependency_overrides[get_session] = runtime_session
        yield SimpleNamespace(runtime=runtime, owner=owner, tenants=tenants, role=role)
    finally:
        admin_app.dependency_overrides.clear()
        gateway_app.dependency_overrides.clear()
        if runtime_engine:
            await runtime_engine.dispose()
        async with owner_engine.begin() as conn:
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        # The production downgrade correctly refuses populated multi-agency state.
        # Discard the disposable test schema instead of bypassing that release safeguard.
        async with owner_engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
        await owner_engine.dispose()


@pytest.mark.asyncio
async def test_runtime_rls_reads_writes_privileges_and_transaction_context(isolated_database):
    db = isolated_database
    a, b = db.tenants[0], db.tenants[2]
    async with db.runtime() as session:
        flags = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert flags == (False, False, False, False, False)
        for table in TENANT_TABLES:
            assert (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one() == 0, table.__tablename__
        assert (await session.execute(select(func.count()).select_from(Price))).scalar_one() == 1
        await set_agency_context(session, a.agency_id)
        for table in TENANT_TABLES:
            assert (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one() == 2, table.__tablename__
        # Short transactions preserve agency context across connection reuse.
        await session.commit()
        assert (await session.execute(select(Client.id).order_by(Client.name))).scalars().all() == [
            t.client.id for t in db.tenants[:2]
        ]
        assert await session.get(Client, b.client.id) is None
        result = await session.execute(
            text("UPDATE clients SET name = 'forged' WHERE id = :foreign"), {"foreign": b.client.id}
        )
        assert result.rowcount == 0
        await session.commit()
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text(
                    "INSERT INTO workflows (client_id, name, risk_class) "
                    "VALUES (:foreign, 'forged', 'R1')"
                ),
                {"foreign": b.client.id},
            )
        await session.rollback()
        # Secondary references cannot splice runs and budgets from different agencies.
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text(
                    "INSERT INTO reservations (budget_id, run_id, est_micro) "
                    "VALUES (:budget, :run, 1)"
                ),
                {"budget": a.budget.id, "run": b.run.id},
            )
        await session.rollback()
        for statement in (
            "UPDATE prices SET input = 0",
            "DELETE FROM prices",
            "INSERT INTO prices (provider, model, effective_from, input, output) "
            "VALUES ('openai','forged','2020-01-01',0,0)",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                await session.execute(text(statement))
            await session.rollback()
        result = await session.execute(
            text("UPDATE kill_flags SET active = true WHERE scope = 'global'")
        )
        assert result.rowcount == 0
        await session.rollback()
    async with db.runtime() as session:
        # Pool checkout must not inherit an agency from the previous request.
        assert (await session.execute(select(func.count()).select_from(Client))).scalar_one() == 0
        await set_agency_context(session, b.agency_id)
        assert (await session.execute(select(Client.id).order_by(Client.name))).scalars().all() == [
            t.client.id for t in db.tenants[2:]
        ]


@pytest.mark.asyncio
async def test_runtime_password_session_key_and_separate_settlement_transactions(isolated_database):
    db = isolated_database
    a, b = db.tenants[0], db.tenants[2]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        for tenant in (a, b):
            login = await admin.post(
                "/admin/auth/login", json={"email": tenant.user.email, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            headers = {"authorization": "Bearer " + login.json()["token"]}
            assert login.json()["agency_id"] == str(tenant.agency_id)
            assert [
                r["id"] for r in (await admin.get("/admin/clients", headers=headers)).json()
            ] == [str(tenant.client.id)]
            assert (
                await admin.get(
                    "/admin/reports/"
                    + str(b.client.id if tenant == a else a.client.id)
                    + "/2026-10",
                    headers=headers,
                )
            ).status_code == 404
        assert (
            await admin.get("/admin/clients", headers={"authorization": "Bearer missing"})
        ).status_code == 401
    with respx.mock(assert_all_called=True) as provider:
        route = provider.post("http://provider.test/v1/chat/completions").respond(
            200,
            json={
                "id": "fixture",
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway"
        ) as gateway:
            for tenant in (a, b):
                response = await gateway.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-mock", "max_tokens": 16},
                    headers={"authorization": "Bearer " + tenant.key, "x-run-id": "live"},
                )
                assert response.status_code == 200, response.text
            assert route.call_count == 2
    async with db.owner() as session:
        for tenant in (a, b):
            budget = await session.get(Budget, tenant.budget.id)
            assert budget.spent_micro == 15 and budget.reserved_micro == 10
