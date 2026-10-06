import httpx
import pytest
from sqlalchemy import update

from admin_api.app import app as admin_app
from core.models import Workflow


@pytest.mark.asyncio
async def test_risk_gates_decision_poll_and_tenant_isolation(governed):
    body = {
        "action_name": "send",
        "action": {"to": "review@example.test", "text": "hello"},
        "summary": "Send message",
    }
    auth = {"authorization": "Bearer " + governed.a.key}
    assert (await governed.http.post("/v1/actions/check", json=body)).status_code == 401
    denied = await governed.http.post("/v1/actions/check", json=body, headers=auth)
    assert denied.json()["decision"] == "deny"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        for risk in ("R0", "R1", "R2", "R3"):
            async with governed.factory() as session:
                await session.execute(
                    update(Workflow)
                    .where(Workflow.id == governed.a.workflow.id)
                    .values(risk_class=risk, policy={"actions": {"send": {}}})
                )
                await session.commit()
            response = await governed.http.post("/v1/actions/check", json=body, headers=auth)
            result = response.json()
            assert result["decision"] == ("allow" if risk < "R2" else "pending")
            identity = result["approval_id"]
            foreign = await governed.http.get(
                "/v1/actions/" + identity, headers={"authorization": "Bearer " + governed.b.key}
            )
            assert foreign.status_code == 404
            if risk >= "R2":
                assert (
                    await admin.post(
                        f"/admin/actions/{identity}/decision",
                        json={"decision": "approve", "approver": "operator"},
                    )
                ).status_code == 401
                approved = await admin.post(
                    f"/admin/actions/{identity}/decision",
                    json={"decision": "approve", "approver": "operator"},
                    headers={"authorization": "Bearer test-admin"},
                )
                assert approved.json()["decision"] == "allow"
                assert (
                    await admin.post(
                        f"/admin/actions/{identity}/decision",
                        json={"decision": "deny", "approver": "operator"},
                        headers={"authorization": "Bearer test-admin"},
                    )
                ).status_code == 409
                assert (await governed.http.get("/v1/actions/" + identity, headers=auth)).json()[
                    "decision"
                ] == "allow"
