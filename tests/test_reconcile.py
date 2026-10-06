import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from admin_api.app import app as admin_app
from core.models import ProviderAccount, ReconcileJob, ReconcileSnapshot
from reconcile.providers import money
from reconcile.service import reconcile

FIXTURES = json.loads((Path(__file__).parent / "fixtures/reporting.json").read_text())


@pytest.mark.asyncio
async def test_daily_provider_usage_cost_units_mapping_and_failure(governed, monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "oa-admin-mock")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "an-admin-mock")
    auth = {"authorization": "Bearer " + governed.a.key}
    async with governed.factory() as session:
        session.add(
            ProviderAccount(client_id=governed.a.client.id, provider="openai", external_id="proj_A")
        )
        session.add(
            ProviderAccount(
                client_id=governed.a.client.id, provider="anthropic", external_id="ws_A"
            )
        )
        await session.commit()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://provider.test/v1/chat/completions").respond(
            200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        )
        assert (
            await governed.http.post(
                "/v1/chat/completions", json={"model": "gpt-mock", "max_tokens": 16}, headers=auth
            )
        ).status_code == 200
        assert route.calls[0].request.headers["OpenAI-Project"] == "proj_A"
        mock.post("http://provider.test/v1/messages").respond(
            200, json={"usage": {"input_tokens": 12, "output_tokens": 3}}
        )
        assert (
            await governed.http.post(
                "/v1/messages",
                json={"model": "claude-mock", "max_tokens": 16},
                headers={"x-api-key": governed.a.key},
            )
        ).status_code == 200
    with respx.mock(assert_all_called=True) as mock:
        endpoints = [
            ("https://api.openai.com/v1/organization/usage/completions", "openai_usage"),
            ("https://api.openai.com/v1/organization/usage/embeddings", "openai_embeddings"),
            ("https://api.openai.com/v1/organization/costs", "openai_costs"),
            ("https://api.anthropic.com/v1/organizations/usage_report/messages", "anthropic_usage"),
            ("https://api.anthropic.com/v1/organizations/cost_report", "anthropic_costs"),
        ]
        for endpoint, fixture in endpoints:
            mock.get(endpoint).respond(200, json=FIXTURES[fixture])
        async with governed.factory() as session:
            for provider in ("openai", "anthropic"):
                account = await session.get(ProviderAccount, (governed.a.client.id, provider))
                assert await reconcile(session, account, datetime.now(UTC).date())
            snapshots = (await session.execute(select(ReconcileSnapshot))).scalars().all()
            assert len(snapshots) == 4
            assert all(row.observed == row.gateway for row in snapshots if row.kind == "usage")
            assert all(
                row.observed["cost_micro"] == 15 and row.gateway["cost_micro"] == 15
                for row in snapshots
                if row.kind == "cost"
            )
            assert not any(row.client_id == governed.b.client.id for row in snapshots)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.openai.com/v1/organization/usage/completions").respond(503)
        async with governed.factory() as session:
            account = await session.get(ProviderAccount, (governed.a.client.id, "openai"))
            assert not await reconcile(session, account, datetime.now(UTC).date())
            job = await session.get(
                ReconcileJob, (governed.a.client.id, "openai", datetime.now(UTC).date())
            )
            assert job.status == "failed"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        duplicate = await admin.post(
            "/admin/provider-accounts",
            headers={"authorization": "Bearer test-admin"},
            json={
                "client_id": str(governed.b.client.id),
                "provider": "openai",
                "external_id": "proj_A",
                "validate_live": False,
            },
        )
        assert duplicate.status_code == 409
    assert money("123.78912", 10000) == 1237892


@pytest.mark.asyncio
async def test_external_usage_flags_possible_bypass_once_and_resolves(governed, monkeypatch):
    import copy

    from audit.chain import verify_chain
    from core.models import Alert, Event

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "oa-admin-mock")
    async with governed.factory() as session:
        account = ProviderAccount(
            client_id=governed.a.client.id, provider="openai", external_id="proj_A"
        )
        session.add(account)
        await session.commit()
        with respx.mock(assert_all_called=True) as mock:
            usage = mock.get("https://api.openai.com/v1/organization/usage/completions").respond(
                200, json=FIXTURES["openai_usage"]
            )
            mock.get("https://api.openai.com/v1/organization/usage/embeddings").respond(
                200, json=FIXTURES["openai_embeddings"]
            )
            costs = mock.get("https://api.openai.com/v1/organization/costs").respond(
                200, json=FIXTURES["openai_costs"]
            )
            for _ in range(2):
                assert await reconcile(session, account, datetime.now(UTC).date())
            alerts = (await session.execute(select(Alert))).scalars().all()
            assert len(alerts) == 1 and alerts[0].kind == "possible_bypass"
            assert alerts[0].details["differences"]["gpt-mock"]["input_tokens"] == 12
            assert alerts[0].client_id == governed.a.client.id
            events = (await session.execute(select(Event))).scalars().all()
            assert [event.type for event in events] == ["drift_detected"]
            empty = copy.deepcopy(FIXTURES["openai_embeddings"])
            usage.respond(200, json=empty)
            costs.respond(200, json=empty)
            assert await reconcile(session, account, datetime.now(UTC).date())
            await session.refresh(alerts[0])
            assert alerts[0].details["resolved"] is True
            assert (await verify_chain(session, governed.a.client.id))[0]
