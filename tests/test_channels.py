import base64
import json
import time
import uuid

import httpx
import pytest
import respx
from sqlalchemy import update

from admin_api.app import app as admin_app
from approvals.channels import signature, verify_signature
from approvals.notifications import dispatch
from core.models import Workflow


@pytest.mark.asyncio
async def test_email_and_signed_webhook_decision_with_replay_rejection(
    governed, monkeypatch, review_team
):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "gateway@example.test")
    monkeypatch.setenv("APPROVAL_EMAIL_TO", "operator@example.test")
    monkeypatch.setenv("APPROVAL_WEBHOOK_URL", "https://hook.test/approval")
    monkeypatch.setenv("WEBHOOK_SIGNING_KEY", base64.b64encode(b"w" * 32).decode())

    async def public_url(url):
        assert url == "https://hook.test/approval"

    monkeypatch.setattr("approvals.channels.public_webhook_url", public_url)
    team = review_team
    for channel in ("email", "webhook"):
        result = await team.admin.post(
            "/admin/integration-destinations",
            headers=team.headers,
            json={
                "name": channel,
                "channel": channel,
                "client_id": team.client_id,
                "group_id": team.group_id,
                "enabled": True,
                "url": "https://hook.test/approval" if channel == "webhook" else None,
                "service_identity": "client-a-service" if channel == "webhook" else None,
            },
        )
        assert result.status_code == 201, result.text
        if channel == "webhook":
            destination = result.json()
    key = destination["credential"].encode()
    endpoint = "/integrations/webhook/" + destination["id"] + "/decision"
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
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(
                risk_class="R2",
                policy={"actions": {"publish": {}}, "approval_channels": ["email", "webhook"]},
            )
        )
        await session.commit()
    result = (
        await governed.http.post(
            "/v1/actions/check",
            headers={"authorization": "Bearer " + governed.a.key},
            json={
                "action_name": "publish",
                "action": {"text": "approved text"},
                "summary": "Publish text",
            },
        )
    ).json()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://hook.test/approval").respond(200)
        async with governed.factory() as session:
            assert await dispatch(session) == 2
        request = route.calls[0].request
        verify_signature(
            request.headers["x-gas-timestamp"],
            request.content,
            request.headers["x-gas-signature"],
            key,
        )
        assert json.loads(request.content)["action"]["text"] == "approved text"
    assert (
        len(messages) == 1
        and "Review and decide:" in messages[0].get_body(preferencelist=("plain",)).get_content()
    )
    raw = json.dumps(
        {
            "event_id": str(uuid.uuid4()),
            "approval_id": result["approval_id"],
            "decision": "approve",
            "approver": "integration",
        }
    ).encode()
    timestamp = str(int(time.time()))
    headers = {
        "authorization": "Bearer " + destination["credential"],
        "x-gas-timestamp": timestamp,
        "x-gas-signature": signature(timestamp, raw, key),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        assert (await admin.post(endpoint, content=raw)).status_code == 401
        assert (await admin.post(endpoint, content=raw + b" ", headers=headers)).status_code == 403
        assert (await admin.post(endpoint, content=raw, headers=headers)).status_code == 200
        assert (await admin.post(endpoint, content=raw, headers=headers)).status_code == 409
        stale = str(int(time.time()) - 600)
        assert (
            await admin.post(
                endpoint,
                content=raw,
                headers={
                    "authorization": "Bearer " + destination["credential"],
                    "x-gas-timestamp": stale,
                    "x-gas-signature": signature(stale, raw, key),
                },
            )
        ).status_code == 403

    from core.models import Approval

    async with governed.factory() as session:
        record = await session.get(Approval, uuid.UUID(result["approval_id"]))
        assert record.approver == "webhook:" + destination["id"] + ":client-a-service"
    assert str(messages[0]["To"]) == team.email
