from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from admin_api.app import app as admin_app
from approvals.expiry import expire_pending
from audit.chain import verify_chain
from core.models import Approval, Event, Workflow


@pytest.mark.asyncio
async def test_all_action_transitions_are_chained_without_plaintext_content(governed):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(risk_class="R2", policy={"actions": {"send": {}}, "approval_channels": []})
        )
        await session.commit()
    auth = {"authorization": "Bearer " + governed.a.key}
    body = {"action_name": "send", "action": {"text": "PRIVATE-CONTENT-TEST"}, "summary": "Review"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        for choice in ("approve", "deny", "expire"):
            result = (await governed.http.post("/v1/actions/check", json=body, headers=auth)).json()
            identity = result["approval_id"]
            if choice == "expire":
                async with governed.factory() as session:
                    await session.execute(
                        update(Approval)
                        .where(Approval.id == identity)
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
                    await session.commit()
                    await expire_pending(session)
            else:
                await admin.post(
                    f"/admin/actions/{identity}/decision",
                    json={"decision": choice, "approver": "operator"},
                    headers={"authorization": "Bearer test-admin"},
                )
    async with governed.factory() as session:
        events = (await session.execute(select(Event).order_by(Event.id))).scalars().all()
        assert [event.type for event in events] == [
            "action_proposed",
            "action_approved",
            "action_proposed",
            "action_denied",
            "action_proposed",
            "action_expired",
        ]
        assert events[1].usage["approver"] == "operator"
        assert all("PRIVATE-CONTENT-TEST" not in str(event.usage) for event in events)
        approvals = (await session.execute(select(Approval))).scalars().all()
        assert all(b"PRIVATE-CONTENT-TEST" not in record.content_ciphertext for record in approvals)
        assert await verify_chain(session, governed.a.client.id) == (True, None)
        assert await verify_chain(session, governed.b.client.id) == (True, None)
        events[1].usage = {"approver": "tampered"}
        await session.flush()
        assert (await verify_chain(session, governed.a.client.id))[0] is False
        await session.rollback()
