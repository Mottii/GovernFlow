from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from approvals.expiry import expire_pending
from core.models import Approval, Event, Workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("risk,outcome", [("R2", "deny"), ("R1", "allow"), ("R1", "deny")])
async def test_expiry_applies_configured_outcome_once(governed, risk, outcome):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(
                risk_class=risk,
                policy={"actions": {"send": {"on_timeout": outcome, "require_approval": True}}},
            )
        )
        await session.commit()
    action = {"text": "hello"}
    auth = {"authorization": "Bearer " + governed.a.key}
    pending = (
        await governed.http.post(
            "/v1/actions/check",
            json={"action_name": "send", "action": action, "summary": "Send"},
            headers=auth,
        )
    ).json()
    assert pending["decision"] == "pending"
    async with governed.factory() as session:
        await session.execute(
            update(Approval).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
        assert await expire_pending(session) == 1
        assert await expire_pending(session) == 0
    result = (await governed.http.get("/v1/actions/" + pending["approval_id"], headers=auth)).json()
    assert result["status"] == "expired" and result["decision"] == outcome
    if outcome == "allow":
        assert (
            await governed.http.post(
                "/v1/actions/consume",
                headers=auth,
                json={"token": result["token"], "action_name": "send", "action": action},
            )
        ).status_code == 200
    async with governed.factory() as session:
        events = (
            (await session.execute(select(Event).where(Event.type == "action_expired")))
            .scalars()
            .all()
        )
        assert len(events) == 1 and events[0].usage["outcome"] == outcome


@pytest.mark.asyncio
async def test_high_risk_timeout_allow_is_rejected(governed):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(risk_class="R3", policy={"actions": {"delete": {"on_timeout": "allow"}}})
        )
        await session.commit()
    response = await governed.http.post(
        "/v1/actions/check",
        json={"action_name": "delete", "action": {}, "summary": "Delete"},
        headers={"authorization": "Bearer " + governed.a.key},
    )
    assert response.status_code == 503
