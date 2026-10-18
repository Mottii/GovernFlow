import asyncio
import time

import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

import gateway.auth
from core.models import Budget, Event, Workflow
from gateway.outage import _cache
from policy.replay import replay


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "risk,mode,known,allowed",
    [
        ("R1", "open", True, True),
        ("R0", "open", True, True),
        ("R2", "open", True, False),
        ("R3", "open", True, False),
        ("R1", "closed", True, False),
        ("R1", "open", False, False),
    ],
)
async def test_outage_requires_known_low_risk_opt_in_and_replays_once(
    governed, monkeypatch, risk, mode, known, allowed
):
    monkeypatch.setenv("ALLOW_FAIL_OPEN", "true")
    monkeypatch.setenv("SELF_HOSTED_SINGLE_NODE", "true")
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(risk_class=risk, policy={"models": ["*"], "outage": {"mode": mode}})
        )
        await session.commit()
    body = {"model": "gpt-mock", "max_tokens": 16}
    headers = {"authorization": "Bearer " + governed.a.key}
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://provider.test/v1/chat/completions").respond(
            200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        )
        if known:
            assert (
                await governed.http.post("/v1/chat/completions", json=body, headers=headers)
            ).status_code == 200

        async def unavailable(*args, **kwargs):
            raise OperationalError("connect", {}, Exception("down"))

        monkeypatch.setattr(gateway.auth, "resolve_virtual_key", unavailable)
        response = await governed.http.post("/v1/chat/completions", json=body, headers=headers)
        assert response.status_code == (200 if allowed else 503)
        assert route.call_count == int(known) + int(allowed)
        if allowed:
            assert response.headers["x-gateway-estimated"] == "false"
            async with governed.factory() as session:
                assert await replay(session) == 2
                assert await replay(session) == 0
                events = (await session.execute(select(Event).order_by(Event.id))).scalars().all()
                assert events[-1].estimated is False
                budget = (
                    await session.execute(
                        select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
                    )
                ).scalar_one()
                assert budget.spent_micro == 30 and budget.reserved_micro == 0
            for snapshot in _cache.values():
                snapshot.expires = time.monotonic() - 1
            response = await governed.http.post("/v1/chat/completions", json=body, headers=headers)
            assert response.status_code == 503


@pytest.mark.asyncio
async def test_outage_terminal_evidence_retries_storage_without_repeating_provider(
    governed, monkeypatch
):
    from gateway.app import app
    from policy import journal

    monkeypatch.setenv("ALLOW_FAIL_OPEN", "true")
    monkeypatch.setenv("SELF_HOSTED_SINGLE_NODE", "true")
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(policy={"models": ["*"], "outage": {"mode": "open"}})
        )
        await session.commit()
    headers = {"authorization": "Bearer " + governed.a.key}
    body = {"model": "gpt-mock", "max_tokens": 16}
    service = app.state.gateway_runtime
    real_persist = journal.persist
    writes = 0

    async def interrupted_store(record, *, store):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("temporary storage failure")
        await real_persist(record, store=store)

    async def unavailable(*args, **kwargs):
        raise OperationalError("connect", {}, Exception("down"))

    with respx.mock as mock:
        route = mock.post("http://provider.test/v1/chat/completions").respond(
            200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        )
        assert (
            await governed.http.post("/v1/chat/completions", json=body, headers=headers)
        ).status_code == 200
        monkeypatch.setattr(gateway.auth, "resolve_virtual_key", unavailable)
        monkeypatch.setattr(journal, "persist", interrupted_store)
        assert (
            await governed.http.post("/v1/chat/completions", json=body, headers=headers)
        ).status_code == 200
        assert service.failed_evidence
        assert (
            await governed.http.post("/v1/chat/completions", json=body, headers=headers)
        ).status_code == 503
        async with asyncio.timeout(3):
            while service.failed_evidence:
                await asyncio.sleep(0.05)
        assert writes == 2 and route.call_count == 2
        async with governed.factory() as session:
            assert await replay(session) == 2
            assert await replay(session) == 0
            budget = (
                await session.execute(
                    select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
                )
            ).scalar_one()
            assert budget.spent_micro == 30 and budget.reserved_micro == 0
