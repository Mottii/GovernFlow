import pytest
import respx
from sqlalchemy import select, update

from audit.chain import verify_chain
from core.models import Alert, Budget, Event, KillFlag, Workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [{"max_steps": 3}, {"run_cost_micro": 90}, {"rpm": 3}])
async def test_loop_soak_trips_kill_and_alert(governed, limit):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(policy={"models": ["*"], "loop_breaker": limit})
        )
        await session.commit()
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://provider.test/v1/chat/completions").respond(
            200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        )
        statuses = []
        for _ in range(15):
            response = await governed.http.post(
                "/v1/chat/completions",
                json={"model": "gpt-mock", "max_tokens": 16},
                headers={"authorization": "Bearer " + governed.a.key, "X-Run-Id": "loop"},
            )
            statuses.append(response.status_code)
        assert statuses.count(200) <= (5 if "run_cost_micro" in limit else 3)
        assert 429 in statuses and statuses[-1] == 503
    async with governed.factory() as session:
        flag = await session.get(KillFlag, ("workflow", str(governed.a.workflow.id)))
        assert flag.active
        assert (await session.execute(select(Alert))).scalars().first().kind == "loop_breaker"
        assert await verify_chain(session, governed.a.client.id) == (True, None)


@pytest.mark.asyncio
async def test_actual_cost_overrun_is_recorded_in_full_and_stops_next_call(governed):
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://provider.test/v1/chat/completions").respond(
            200, json={"usage": {"prompt_tokens": 900, "completion_tokens": 100}}
        )
        body = {"model": "gpt-mock", "max_tokens": 1}
        headers = {"authorization": "Bearer " + governed.a.key}
        assert (
            await governed.http.post("/v1/chat/completions", json=body, headers=headers)
        ).status_code == 200
        assert (
            await governed.http.post("/v1/chat/completions", json=body, headers=headers)
        ).status_code == 503
        assert route.call_count == 1
    async with governed.factory() as session:
        budget = (
            await session.execute(
                select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
            )
        ).scalar_one()
        assert (budget.spent_micro, budget.reserved_micro) == (1000, 0)
        events = (await session.execute(select(Event).order_by(Event.id))).scalars().all()
        assert [event.type for event in events] == ["provider_call", "budget_overrun"]
        assert await verify_chain(session, governed.a.client.id) == (True, None)
