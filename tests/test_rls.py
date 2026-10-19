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
from sqlalchemy import func, select, text, update
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
                        policy_version=1,
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
                    from admin_api.credentials import add_version
                    from core.models import PolicyRevision
                    from policy.revisions import digest, normalize

                    normalized = normalize(workflow.policy, workflow.risk_class)
                    session.add(
                        PolicyRevision(
                            agency_id=agency_id,
                            client_id=client.id,
                            workflow_id=workflow.id,
                            version=1,
                            risk_class=workflow.risk_class,
                            policy=normalized,
                            content_hash=digest(normalized, workflow.risk_class),
                            actor="fixture",
                        )
                    )
                    slot = (
                        await session.execute(
                            select(ProviderCredential).where(
                                ProviderCredential.client_id == client.id
                            )
                        )
                    ).scalar_one()
                    await add_version(session, slot, "mock-only", "fixture", "active")
                    approval = Approval(
                        policy_version=1,
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
        yield SimpleNamespace(
            runtime=runtime, owner=owner, tenants=tenants, role=role, runtime_url=runtime_url
        )
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
        with pytest.raises(DBAPIError, match="row-level security|Agency ownership|foreign key"):
            await session.execute(
                text(
                    "INSERT INTO workflows (client_id, name, risk_class) "
                    "VALUES (:foreign, 'forged', 'R1')"
                ),
                {"foreign": b.client.id},
            )
        await session.rollback()
        # Secondary references cannot splice runs and budgets from different agencies.
        with pytest.raises(DBAPIError, match="row-level security|Agency ownership|foreign key"):
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


@pytest.mark.asyncio
async def test_scoped_reviews_destinations_prices_and_workers_on_runtime_login(
    isolated_database, monkeypatch
):
    import json
    import time

    from sqlalchemy import update

    from approvals.channels import signature
    from approvals.notifications import callback, dispatch, review_link
    from core.models import (
        AgencyPrice,
        IntegrationDestination,
        ReviewerAssignment,
        ReviewerGroup,
        ReviewerGroupMember,
        TelegramLinkChallenge,
    )
    from policy.budget import current_price

    db = isolated_database
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "fixture-transport-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-bot")
    monkeypatch.setenv("TELEGRAM_APPROVER_CHAT_IDS", "999")
    monkeypatch.setenv("APPROVAL_EMAIL_TO", "global@forbidden.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "sender@example.test")
    messages = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, **kwargs):
            pass

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr("smtplib.SMTP", SMTP)

    async def public_url(url):
        assert url == "https://hook.test/approval"

    monkeypatch.setattr("approvals.channels.public_webhook_url", public_url)
    async with db.owner() as session:
        for index in (0, 2):
            tenant = db.tenants[index]
            user = User(
                email=f"admin{index}@fixture.test",
                role="admin",
                password_hash=b"fixture",
                password_salt=b"fixture",
            )
            session.add(user)
            await session.flush()
            session.add(
                AgencyMembership(
                    agency_id=tenant.agency_id, user_id=user.id, role="admin", all_clients=True
                )
            )
            await session.flush()
            session.add(
                AdminSession(
                    token_hash=token_hash(f"admin-{index}"),
                    user_id=user.id,
                    agency_id=tenant.agency_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        for tenant in db.tenants:
            await session.execute(
                update(AgencyMembership)
                .where(
                    AgencyMembership.agency_id == tenant.agency_id,
                    AgencyMembership.user_id == tenant.user.id,
                )
                .values(role="approver")
            )
        await session.commit()
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway"
        ) as gateway,
    ):
        destinations = []
        groups = []
        checks = []
        for i, tenant in enumerate(db.tenants):
            headers = {"authorization": f"Bearer admin-{0 if i < 2 else 2}"}
            actor = {"authorization": "Bearer session-" + tenant.client.name}
            group = await admin.post(
                "/admin/reviewer-groups",
                headers=headers,
                json={
                    "client_id": str(tenant.client.id),
                    "name": "Assigned team",
                    "user_ids": [str(tenant.user.id)],
                },
            )
            assert group.status_code == 201, group.text
            group_id = group.json()["id"]
            groups.append(group_id)
            assert (
                await admin.put(
                    f"/admin/reviewer-groups/{group_id}/assignments",
                    headers=headers,
                    json={"workflow_ids": [str(tenant.workflow.id)]},
                )
            ).status_code == 200
            for channel in ("email", "telegram", "webhook"):
                result = await admin.post(
                    "/admin/integration-destinations",
                    headers=headers,
                    json={
                        "name": channel,
                        "channel": channel,
                        "client_id": str(tenant.client.id),
                        "group_id": group_id,
                        "workflow_id": str(tenant.workflow.id),
                        "enabled": True,
                        "url": "https://hook.test/approval" if channel == "webhook" else None,
                        "service_identity": tenant.client.name if channel == "webhook" else None,
                    },
                )
                assert result.status_code == 201, result.text
                if channel == "webhook":
                    destinations.append(result.json())
            challenge = await admin.post("/admin/telegram/link", headers=actor)
            assert challenge.status_code == 201, challenge.text
            chat = 101 + i
            linked = await admin.post(
                "/integrations/telegram",
                headers={"x-telegram-bot-api-secret-token": "fixture-transport-secret"},
                json={
                    "message": {
                        "from": {"id": chat},
                        "chat": {"id": chat, "type": "private"},
                        "text": challenge.json()["command"],
                    }
                },
            )
            assert linked.status_code == 200, linked.text
            check = await gateway.post(
                "/v1/actions/check",
                headers={"authorization": "Bearer " + tenant.key},
                json={
                    "action_name": "publish",
                    "summary": "Private " + tenant.client.name,
                    "action": {"private": tenant.client.name},
                },
            )
            assert check.status_code == 200, check.text
            checks.append(check.json()["approval_id"])
        retarget = await admin.put(
            "/admin/integration-destinations/" + destinations[0]["id"],
            headers={"authorization": "Bearer admin-0"},
            json={
                "name": "retargeted",
                "channel": "webhook",
                "client_id": str(db.tenants[1].client.id),
                "enabled": True,
                "url": "https://hook.test/approval",
                "service_identity": "A2",
            },
        )
        assert retarget.status_code == 400
        for i in (0, 2):
            headers = {"authorization": f"Bearer admin-{i}"}
            result = await admin.post(
                "/admin/agency-prices",
                headers=headers,
                json={
                    "provider": "openai",
                    "model": "gpt-mock",
                    "effective_from": "2020-01-01",
                    "input": 100 + i,
                    "output": 200 + i,
                    "source_url": "https://fixture.test/price",
                },
            )
            assert result.status_code == 201, result.text
            assert (await admin.post("/admin/prices/seed", headers=headers)).status_code == 403
            assert len((await admin.get("/admin/agency-prices", headers=headers)).json()) == 1
            foreign = destinations[2 if i == 0 else 0]
            assert (
                await admin.put(
                    "/admin/integration-destinations/" + foreign["id"],
                    headers=headers,
                    json={"name": "forged", "channel": "webhook"},
                )
            ).status_code == 404
        tables = (
            ReviewerGroup,
            ReviewerGroupMember,
            ReviewerAssignment,
            IntegrationDestination,
            TelegramLinkChallenge,
            AgencyPrice,
        )
        async with db.runtime() as session:
            for table in tables:
                assert (
                    await session.execute(select(func.count()).select_from(table))
                ).scalar_one() == 0
        with respx.mock(assert_all_called=True) as mock:
            tg = mock.post("https://api.telegram.org/botmock-bot/sendMessage").respond(
                200, json={"ok": True}
            )
            hooks = mock.post("https://hook.test/approval").respond(200)
            for i in (0, 2):
                async with db.runtime() as session:
                    await set_agency_context(session, db.tenants[i].agency_id)
                    assert await dispatch(session) == 6
                    assert await dispatch(session) == 0
                    assert (await current_price(session, "openai", "gpt-mock")).input == 100 + i
                    assert (
                        await session.execute(
                            select(func.count()).select_from(IntegrationDestination)
                        )
                    ).scalar_one() == 6
                    foreign = db.tenants[2 if i == 0 else 0]
                    with pytest.raises(DBAPIError):
                        await session.execute(
                            text(
                                "INSERT INTO reviewer_assignments "
                                "(id,agency_id,client_id,group_id,workflow_id) "
                                "VALUES(gen_random_uuid(),:agency,:client,:group,:workflow)"
                            ),
                            {
                                "agency": db.tenants[i].agency_id,
                                "client": db.tenants[i].client.id,
                                "group": uuid.UUID(groups[i]),
                                "workflow": foreign.workflow.id,
                            },
                        )
                    await session.rollback()
            assert tg.call_count == 4 and hooks.call_count == 4
            assert {json.loads(c.request.content)["chat_id"] for c in tg.calls} == {
                str(101 + i) for i in range(4)
            }
        assert {str(m["To"]) for m in messages} == {t.user.email for t in db.tenants}
        for i, tenant in enumerate(db.tenants):
            actor = {"authorization": "Bearer session-" + tenant.client.name}
            async with db.runtime() as session:
                await set_agency_context(session, tenant.agency_id)
                record = await session.get(Approval, uuid.UUID(checks[i]))
                link = review_link(record, "email:forwarded")
            assert (await admin.get(link, headers=actor)).status_code == 200
            foreign_actor = {
                "authorization": "Bearer session-" + db.tenants[(i + 1) % 4].client.name
            }
            assert (await admin.get(link, headers=foreign_actor)).status_code == 404
            from urllib.parse import parse_qs, urlsplit

            token = parse_qs(urlsplit(link).query)["token"][0]
            assert (
                await admin.post(
                    "/review/" + checks[i],
                    headers=foreign_actor,
                    data={"token": token, "decision": "approve"},
                )
            ).status_code == 404
            wrong = 102 + i if i < 3 else 101
            forwarded = {
                "callback_query": {
                    "from": {"id": wrong},
                    "message": {"chat": {"id": wrong}},
                    "data": callback(uuid.UUID(checks[i]), "a", str(wrong)),
                }
            }
            assert (
                await admin.post(
                    "/integrations/telegram",
                    headers={"x-telegram-bot-api-secret-token": "fixture-transport-secret"},
                    json=forwarded,
                )
            ).status_code in (403, 404)
        # Removal invalidates an already delivered link and a signed Telegram callback.
        headers = {"authorization": "Bearer admin-0"}
        assert (
            await admin.put(
                f"/admin/reviewer-groups/{groups[0]}/assignments",
                headers=headers,
                json={"workflow_ids": []},
            )
        ).status_code == 200
        async with db.runtime() as session:
            await set_agency_context(session, db.tenants[0].agency_id)
            link = review_link(await session.get(Approval, uuid.UUID(checks[0])), "email:old")
        assert (
            await admin.get(link, headers={"authorization": "Bearer session-A1"})
        ).status_code == 403
        tg_body = {
            "callback_query": {
                "from": {"id": 101},
                "message": {"chat": {"id": 101}},
                "data": callback(uuid.UUID(checks[0]), "a", "101"),
            }
        }
        assert (
            await admin.post(
                "/integrations/telegram",
                headers={"x-telegram-bot-api-secret-token": "fixture-transport-secret"},
                json=tg_body,
            )
        ).status_code == 403
        # Destinations cannot choose another client or submit their own reviewer identity.
        for i, destination in enumerate(destinations):
            key = destination["credential"].encode()
            endpoint = "/integrations/webhook/" + destination["id"] + "/decision"

            async def decision(approval_id, endpoint=endpoint, destination=destination, key=key):
                raw = json.dumps(
                    {
                        "event_id": str(uuid.uuid4()),
                        "approval_id": approval_id,
                        "decision": "approve",
                        "approver": "forged-admin",
                    }
                ).encode()
                timestamp = str(int(time.time()))
                return await admin.post(
                    endpoint,
                    content=raw,
                    headers={
                        "authorization": "Bearer " + destination["credential"],
                        "x-gas-timestamp": timestamp,
                        "x-gas-signature": signature(timestamp, raw, key),
                    },
                )

            assert (await decision(checks[(i + 1) % 4])).status_code in (403, 404)
            result = await decision(checks[i])
            assert result.status_code == 200, result.text
        # Both human channels also record valid decisions under the restricted login.
        for i, channel in ((1, "telegram"), (2, "email")):
            tenant = db.tenants[i]
            checked = await gateway.post(
                "/v1/actions/check",
                headers={"authorization": "Bearer " + tenant.key},
                json={
                    "action_name": "publish",
                    "summary": "Human decision",
                    "action": {"client": tenant.client.name},
                },
            )
            identity = checked.json()["approval_id"]
            if channel == "telegram":
                response = await admin.post(
                    "/integrations/telegram",
                    headers={"x-telegram-bot-api-secret-token": "fixture-transport-secret"},
                    json={
                        "callback_query": {
                            "from": {"id": 102},
                            "message": {"chat": {"id": 102}},
                            "data": callback(uuid.UUID(identity), "a", "102"),
                        }
                    },
                )
                approver = "telegram:" + str(tenant.user.id)
            else:
                async with db.runtime() as session:
                    await set_agency_context(session, tenant.agency_id)
                    link = review_link(
                        await session.get(Approval, uuid.UUID(identity)), "email:personal"
                    )
                from urllib.parse import parse_qs, urlsplit

                token = parse_qs(urlsplit(link).query)["token"][0]
                response = await admin.post(
                    "/review/" + identity,
                    headers={"authorization": "Bearer session-" + tenant.client.name},
                    data={"token": token, "decision": "approve"},
                )
                approver = tenant.user.email
            assert response.status_code == 200, response.text
            async with db.owner() as session:
                assert (await session.get(Approval, uuid.UUID(identity))).approver == approver
        async with db.owner() as session:
            for i, tenant in enumerate(db.tenants):
                record = await session.get(Approval, uuid.UUID(checks[i]))
                assert (
                    record.approver == "webhook:" + destinations[i]["id"] + ":" + tenant.client.name
                )


@pytest.mark.asyncio
async def test_runtime_configuration_refuses_owner_and_accepts_restricted_login(
    isolated_database, monkeypatch
):
    from core.database import make_engine

    monkeypatch.delenv("MAINTENANCE_MODE", raising=False)
    owner = make_engine(os.environ["TEST_DATABASE_URL"])
    runtime = make_engine(str(isolated_database.runtime_url.render_as_string(hide_password=False)))
    try:
        with pytest.raises(RuntimeError, match="restricted non-owner"):
            async with owner.connect():
                pass
        async with runtime.connect() as connection:
            assert (
                await connection.execute(text("SELECT count(*) FROM clients"))
            ).scalar_one() == 0
            assert (
                await connection.execute(text("SELECT current_user"))
            ).scalar_one() == isolated_database.role
    finally:
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.asyncio
async def test_background_expiry_reaping_and_directory_keep_agency_context(
    isolated_database, monkeypatch
):
    from sqlalchemy import update

    import core.database as database
    from approvals.expiry import expire_pending
    from policy.reap import reap_stale

    db = isolated_database
    past = datetime.now(UTC) - timedelta(hours=2)
    async with db.owner() as session:
        await session.execute(update(Approval).values(expires_at=past))
        await session.execute(update(Reservation).values(created_at=past))
        await session.commit()
    monkeypatch.setattr(database, "session_factory", db.runtime)
    visited = []
    async for session in database.agency_sessions():
        agency_id = session.info["agency_id"]
        visited.append(agency_id)
        assert await expire_pending(session) == 2
        assert await reap_stale(session, datetime.now(UTC) - timedelta(hours=1)) == 2
        assert (
            await session.execute(
                select(func.count()).select_from(Approval).where(Approval.status == "expired")
            )
        ).scalar_one() == 2
        async with db.owner() as owner:
            for tenant in db.tenants:
                approval = await owner.get(Approval, tenant.approval.id)
                budget = await owner.get(Budget, tenant.budget.id)
                handled = tenant.agency_id in visited
                assert approval.status == ("expired" if handled else "pending")
                assert budget.reserved_micro == (0 if handled else 10)
                assert budget.spent_micro == (10 if handled else 0)
    assert set(visited) == {t.agency_id for t in db.tenants}


@pytest.mark.asyncio
async def test_provider_accounting_corrections_and_recovery_obey_runtime_scopes(isolated_database):
    from sqlalchemy.exc import DBAPIError

    from core.models import AccountingReceipt, AccountingRecovery, ChargeEntry, ProviderCall
    from policy.accounting import Evidence
    from policy.journal import persist
    from policy.replay import replay

    db = isolated_database
    async with db.owner() as session:
        for i in (0, 2):
            tenant = db.tenants[i]
            user = User(
                email=f"accounting-admin-{i}@fixture.test",
                role="admin",
                password_hash=b"fixture",
                password_salt=b"fixture",
            )
            session.add(user)
            await session.flush()
            session.add(
                AgencyMembership(
                    agency_id=tenant.agency_id, user_id=user.id, role="admin", all_clients=True
                )
            )
            await session.flush()
            session.add(
                AdminSession(
                    agency_id=tenant.agency_id,
                    user_id=user.id,
                    token_hash=token_hash(f"accounting-admin-{i}"),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await session.commit()
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway"
        ) as gateway,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
        ) as admin,
    ):
        ids = []
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://provider.test/v1/chat/completions").respond(
                200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
            )
            for tenant in db.tenants:
                result = await gateway.post(
                    "/v1/chat/completions",
                    headers={"authorization": "Bearer " + tenant.key},
                    json={"model": "gpt-mock", "max_tokens": 16},
                )
                assert result.status_code == 200, result.text
                ids.append(uuid.UUID(result.headers["x-gateway-call-id"]))
        for i, tenant in enumerate(db.tenants):
            viewer = {"authorization": "Bearer session-" + tenant.client.name}
            own = await admin.get(f"/admin/provider-calls/{ids[i]}", headers=viewer)
            assert own.status_code == 200 and own.json()["charged_micro"] == 15
            assert len((await admin.get("/admin/provider-calls", headers=viewer)).json()) == 1
            foreign = ids[(i + 1) % 4]
            assert (
                await admin.get(f"/admin/provider-calls/{foreign}", headers=viewer)
            ).status_code == 404
            body = {
                "evidence_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "measured_micro": 17,
                "reason": "Invoice adjustment",
                "evidence_ref": "invoice:fixture",
            }
            assert (
                await admin.post(
                    f"/admin/provider-calls/{ids[i]}/corrections", headers=viewer, json=body
                )
            ).status_code == 403
            operator = {"authorization": f"Bearer accounting-admin-{0 if i < 2 else 2}"}
            result = await admin.post(
                f"/admin/provider-calls/{ids[i]}/corrections", headers=operator, json=body
            )
            assert result.status_code == 200, result.text
            other_agency = ids[2 if i < 2 else 0]
            assert (
                await admin.post(
                    f"/admin/provider-calls/{other_agency}/corrections", headers=operator, json=body
                )
            ).status_code == 404
            for format in ("json", "csv", "html"):
                report = await admin.get(
                    f"/admin/reports/{tenant.client.id}/{datetime.now(UTC):%Y-%m}",
                    headers=viewer,
                    params={"format": format},
                )
                assert report.status_code == 200, report.text
                if format == "json":
                    assert report.json()["totals"]["cost_micro"] == 17
                    assert report.json()["totals"]["calls"] == 1
            # A stale estimate is superseded by the stronger measured administrator result.
            await persist(
                Evidence(
                    evidence_id=uuid.uuid4(),
                    agency_id=tenant.agency_id,
                    client_id=tenant.client.id,
                    call_id=ids[i],
                    kind="estimate",
                    amount_micro=100,
                    reason="expired_lease",
                ).model_dump(mode="json")
            )
        async with db.runtime() as session:
            assert await replay(session) == 4
        for i in (0, 2):
            async with db.runtime() as session:
                for table in (ProviderCall, ChargeEntry, AccountingReceipt, AccountingRecovery):
                    assert (
                        await session.execute(select(func.count()).select_from(table))
                    ).scalar_one() == 0
                await set_agency_context(session, db.tenants[i].agency_id)
                assert (
                    await session.execute(select(func.count()).select_from(ProviderCall))
                ).scalar_one() == 2
                assert (
                    await session.execute(select(func.count()).select_from(ChargeEntry))
                ).scalar_one() == 4
                assert (
                    await session.execute(select(func.count()).select_from(AccountingRecovery))
                ).scalar_one() == 2
                assert await session.get(ProviderCall, ids[2 if i == 0 else 0]) is None
                with pytest.raises(DBAPIError, match="permission denied"):
                    await session.execute(text("DELETE FROM charge_entries"))
                await session.rollback()


@pytest.mark.asyncio
async def test_phase3_controls_and_immutable_history_on_restricted_login(isolated_database):
    from audit.administration import record
    from core.models import AdministrativeEvent, ClientBudget, CredentialVersion, PolicyRevision

    db = isolated_database
    own, other_client, foreign = db.tenants[0], db.tenants[1], db.tenants[2]
    async with db.owner() as session:
        admin_user = User(
            email="phase3-admin@fixture.test",
            role="admin",
            password_hash=b"fixture",
            password_salt=b"fixture",
        )
        operator_user = User(
            email="phase3-operator@fixture.test",
            role="operator",
            password_hash=b"fixture",
            password_salt=b"fixture",
        )
        session.add_all([admin_user, operator_user])
        await session.flush()
        session.add_all(
            [
                AgencyMembership(
                    agency_id=own.agency_id, user_id=admin_user.id, role="admin", all_clients=True
                ),
                AgencyMembership(
                    agency_id=own.agency_id,
                    user_id=operator_user.id,
                    role="operator",
                    all_clients=False,
                ),
            ]
        )
        await session.flush()
        session.add(
            ClientAssignment(
                agency_id=own.agency_id, user_id=operator_user.id, client_id=own.client.id
            )
        )
        for user, token in [(admin_user, "phase3-admin"), (operator_user, "phase3-operator")]:
            session.add(
                AdminSession(
                    agency_id=own.agency_id,
                    user_id=user.id,
                    token_hash=token_hash(token),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await session.commit()
    admin_headers = {"authorization": "Bearer phase3-admin"}
    operator_headers = {"authorization": "Bearer phase3-operator"}
    viewer_headers = {"authorization": "Bearer session-A1"}
    body = {
        "period": f"{datetime.now(UTC):%Y-%m}",
        "cap_micro": 100,
        "expected_revision": 1,
        "reason": "Agency cap",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        cap = await admin.put(
            f"/admin/clients/{own.client.id}/budget", headers=operator_headers, json=body
        )
        assert cap.status_code == 200, cap.text
        assert cap.json()["reserved_micro"] == 10
        assert (
            await admin.put(
                f"/admin/clients/{other_client.client.id}/budget",
                headers=operator_headers,
                json=body,
            )
        ).status_code == 404
        assert (
            await admin.put(
                f"/admin/clients/{foreign.client.id}/budget", headers=admin_headers, json=body
            )
        ).status_code == 404
        assert (
            await admin.put(
                f"/admin/clients/{own.client.id}/budget", headers=viewer_headers, json=body
            )
        ).status_code == 403
        assert len((await admin.get("/admin/client-budgets", headers=viewer_headers)).json()) == 1
        draft = await admin.put(
            f"/admin/workflows/{own.workflow.id}/policy",
            headers=operator_headers,
            json={
                "expected_active_version": 1,
                "risk_class": "R2",
                "policy": {"models": ["*"], "rules": []},
                "reason": "Draft",
            },
        )
        assert draft.status_code == 201, draft.text
        version = draft.json()["version"]
        activate = f"/admin/workflows/{own.workflow.id}/policy-revisions/{version}/activate"
        assert (
            await admin.post(
                activate,
                headers=operator_headers,
                json={"expected_active_version": 1, "reason": "Not allowed"},
            )
        ).status_code == 403
        assert (
            await admin.post(
                activate,
                headers=admin_headers,
                json={"expected_active_version": 1, "reason": "Reviewed"},
            )
        ).status_code == 200
        assert (
            await admin.get(
                f"/admin/workflows/{foreign.workflow.id}/policy-revisions", headers=admin_headers
            )
        ).status_code == 404
        assert (
            await admin.get("/admin/administrative-events", headers=operator_headers)
        ).status_code == 403
        events = await admin.get("/admin/administrative-events", headers=admin_headers)
        assert events.status_code == 200 and len(events.json()) >= 3
        credentials = (
            await admin.get("/admin/provider-credentials", headers=viewer_headers)
        ).json()
        assert {c["client_id"] for c in credentials} == {str(own.client.id)}
        slot = credentials[0]
        staged = await admin.post(
            f"/admin/provider-credentials/{slot['id']}/versions",
            headers=operator_headers,
            json={"key": "mock-new-version", "expected_version": 1, "reason": "Rotate fixture"},
        )
        assert staged.status_code == 201, staged.text
        assert "mock-new-version" not in staged.text
        assert (
            await admin.post(
                f"/admin/provider-credentials/{slot['id']}/versions/2/activate",
                headers=operator_headers,
                json={"expected_active_version": 1, "reason": "Activate"},
            )
        ).status_code == 200
    async with db.runtime() as session:
        assert not (await session.execute(select(ClientBudget))).scalars().all()
        await set_agency_context(session, own.agency_id)
        assert len((await session.execute(select(ClientBudget))).scalars().all()) == 1
        with pytest.raises(DBAPIError):
            await session.execute(update(PolicyRevision).values(actor="tampered"))
        await session.rollback()
        await set_agency_context(session, own.agency_id)
        bucket = (await session.execute(select(ClientBudget))).scalar_one()
        old_cap = bucket.cap_micro
        old_events = (
            await session.execute(select(func.count()).select_from(AdministrativeEvent))
        ).scalar_one()
        bucket.cap_micro = 999
        await record(
            session,
            own.agency_id,
            "rollback-test",
            "client_budget",
            bucket.id,
            "cap_changed",
            cap_micro=999,
            reason="Abort together",
        )
        await session.rollback()
        await set_agency_context(session, own.agency_id)
        assert (await session.execute(select(ClientBudget.cap_micro))).scalar_one() == old_cap
        assert (
            await session.execute(select(func.count()).select_from(AdministrativeEvent))
        ).scalar_one() == old_events
        await set_agency_context(session, own.agency_id)
        with pytest.raises(DBAPIError, match="re-encryption requires maintenance"):
            await session.execute(update(CredentialVersion).values(nonce=b"z" * 12))
        await session.rollback()
