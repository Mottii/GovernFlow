import asyncio

import httpx
import pytest
from sqlalchemy import update

from admin_api.app import app as admin_app
from core.models import Workflow
from sdks.python.governed_automation import ActionBlocked, Gateway


@pytest.mark.asyncio
async def test_gateway_single_use_action_binding_and_sdk(governed):
    action = {"recipient": "person@example.test", "body": "hello"}
    auth = {"authorization": "Bearer " + governed.a.key}
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(risk_class="R2", policy={"actions": {"send": {}}})
        )
        await session.commit()
    sdk = Gateway("http://gateway.test", governed.a.key, client=governed.http)
    executed = []

    async def execute():
        executed.append(True)

    with pytest.raises(ActionBlocked):
        await sdk.execute("send", action, "Send", execute, wait_timeout=0)
    assert not executed
    pending = await sdk.check_action("send", action, "Send")
    identity = pending["approval_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        assert (
            await admin.post(
                f"/admin/actions/{identity}/decision",
                json={"decision": "approve", "approver": "operator"},
                headers={"authorization": "Bearer test-admin"},
            )
        ).status_code == 200
    result = (await governed.http.get("/v1/actions/" + identity, headers=auth)).json()
    token = result["token"]
    body = {"token": token, "action_name": "send", "action": action}
    assert (
        await governed.http.post(
            "/v1/actions/consume", json={**body, "action": {"recipient": "attacker"}}, headers=auth
        )
    ).status_code == 403
    assert (
        await governed.http.post(
            "/v1/actions/consume", json=body, headers={"authorization": "Bearer " + governed.b.key}
        )
    ).status_code == 403
    assert (
        await governed.http.post(
            "/v1/actions/consume", json={**body, "token": token + "bad"}, headers=auth
        )
    ).status_code == 403
    responses = await asyncio.gather(
        *(governed.http.post("/v1/actions/consume", json=body, headers=auth) for _ in range(20))
    )
    assert [response.status_code for response in responses].count(200) == 1
    assert [response.status_code for response in responses].count(409) == 19
    assert (await governed.http.get("/v1/actions/" + identity, headers=auth)).json()[
        "decision"
    ] == "deny"
    async with governed.factory() as session:
        await session.execute(
            update(Workflow).where(Workflow.id == governed.a.workflow.id).values(risk_class="R1")
        )
        await session.commit()
    low = await sdk.check_action("send", action, "Send")
    assert await sdk.verify_token(low["token"], "send", action)
    await sdk.execute("send", action, "Send", execute, wait_timeout=0)
    assert executed == [True]
