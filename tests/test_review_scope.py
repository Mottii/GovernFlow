import base64
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, update

from admin_api.app import app
from approvals.channels import signature
from approvals.notifications import callback, review_link
from core.models import Agency, Approval, Client, Notification, VirtualKey, Workflow
from core.security import virtual_key_hash


@pytest.mark.asyncio
async def test_forwarded_review_link_requires_current_authorized_membership(governed):
    master = {"authorization": "Bearer test-admin"}
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(
                risk_class="R2",
                policy={"actions": {"publish": {}}, "approval_channels": []},
            )
        )
        await session.commit()
    checked = await governed.http.post(
        "/v1/actions/check",
        headers={"authorization": "Bearer " + governed.a.key},
        json={"action_name": "publish", "summary": "Review", "action": {"text": "sensitive"}},
    )
    approval_id = checked.json()["approval_id"]
    async with governed.factory() as session:
        record = await session.get(Approval, uuid.UUID(approval_id))
        link = review_link(record, "email:forged-reviewer")
    token = parse_qs(urlparse(link).query)["token"][0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin"
    ) as admin:
        assert (await admin.get(link)).status_code == 401
        assert (
            await admin.post("/review/" + approval_id, data={"token": token, "decision": "approve"})
        ).status_code == 401
        actors = {}
        for label, role, client in (
            ("allowed", "approver", governed.a.client),
            ("foreign", "approver", governed.b.client),
            ("viewer", "viewer", governed.a.client),
        ):
            assert (
                await admin.post(
                    "/admin/users",
                    headers=master,
                    json={
                        "email": label + "@review.test",
                        "password": "review acceptance password",
                        "role": role,
                        "client_id": str(client.id),
                    },
                )
            ).status_code == 201
            login = await admin.post(
                "/admin/auth/login",
                json={"email": label + "@review.test", "password": "review acceptance password"},
            )
            actors[label] = {"authorization": "Bearer " + login.json()["token"]}
        for label, code in (("foreign", 404), ("viewer", 403)):
            assert (await admin.get(link, headers=actors[label])).status_code == code
            assert (
                await admin.post(
                    "/review/" + approval_id,
                    headers=actors[label],
                    data={"token": token, "decision": "approve"},
                )
            ).status_code == code
        assert (await admin.get(link, headers=actors["allowed"])).status_code == 200
        assert (
            await admin.post(
                "/review/" + approval_id,
                headers=actors["allowed"],
                data={"token": token, "decision": "approve"},
            )
        ).status_code == 200
    async with governed.factory() as session:
        record = await session.get(Approval, uuid.UUID(approval_id))
        assert record.approver == "allowed@review.test"


@pytest.mark.asyncio
async def test_new_agencies_do_not_use_legacy_global_notification_authority(governed, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SIGNING_KEY", base64.b64encode(b"w" * 32).decode())
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "legacy-hook")
    monkeypatch.setenv("TELEGRAM_APPROVER_CHAT_IDS", "42")
    key = "gk_" + "C" * 32
    async with governed.factory() as session:
        agency = Agency(name="Agency C")
        session.add(agency)
        await session.flush()
        client = Client(agency_id=agency.id, name="C")
        session.add(client)
        await session.flush()
        workflow = Workflow(
            client_id=client.id,
            name="C",
            risk_class="R2",
            policy={
                "actions": {"publish": {}},
                "approval_channels": ["email", "telegram", "webhook"],
            },
        )
        session.add(workflow)
        await session.flush()
        session.add(
            VirtualKey(workflow_id=workflow.id, key_hash=virtual_key_hash(key), prefix=key[:8])
        )
        await session.commit()
    result = await governed.http.post(
        "/v1/actions/check",
        headers={"authorization": "Bearer " + key},
        json={"action_name": "publish", "summary": "Agency C only", "action": {"text": "secret"}},
    )
    assert result.status_code == 200
    approval_id = uuid.UUID(result.json()["approval_id"])
    async with governed.factory() as session:
        assert (
            not (
                await session.execute(
                    select(Notification).where(Notification.approval_id == approval_id)
                )
            )
            .scalars()
            .all()
        )
        record = await session.get(Approval, approval_id)
        link = review_link(record, "email:legacy")
    import json

    body = json.dumps(
        {
            "event_id": str(uuid.uuid4()),
            "approval_id": str(approval_id),
            "decision": "approve",
            "approver": "forged",
        }
    ).encode()
    timestamp = str(int(time.time()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin"
    ) as admin:
        assert (
            await admin.get(link, headers={"authorization": "Bearer test-admin"})
        ).status_code == 404
        assert (
            await admin.post(
                "/integrations/webhook/decision",
                content=body,
                headers={
                    "x-gas-timestamp": timestamp,
                    "x-gas-signature": signature(timestamp, body),
                },
            )
        ).status_code == 404
        assert (
            await admin.post(
                "/integrations/telegram",
                headers={"x-telegram-bot-api-secret-token": "legacy-hook"},
                json={
                    "callback_query": {
                        "from": {"id": 42},
                        "message": {"chat": {"id": 42}},
                        "data": callback(approval_id, "a", "42"),
                    },
                },
            )
        ).status_code == 404
    async with governed.factory() as session:
        record = await session.get(Approval, approval_id)
        assert record.status == "pending" and record.approver is None
