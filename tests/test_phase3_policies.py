import asyncio
import base64
import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

from admin_api.app import app as admin_app
from admin_api.auth import Principal, require_admin
from approvals.notifications import callback
from approvals.signing import b64, sign, verify
from core.models import AdministrativeEvent, PolicyRevision
from gateway.errors import GatewayError
from policy.revisions import evaluate, normalize


def rule(op, value, *, path="/value", outcome="allow"):
    return {
        "id": "test",
        "action_name": "send",
        "outcome": outcome,
        "conditions": [{"path": path, "op": op, "value": value}],
    }


@pytest.mark.parametrize(
    "op,expected,operand",
    [
        ("exists", True, 1),
        ("eq", 7, 7),
        ("in", [1, 7], 7),
        ("lt", 8, 7),
        ("lte", 7, 7),
        ("gt", 6, 7),
        ("gte", 7, 7),
        ("recipient_domain_in", ["example.test"], "person@example.test"),
    ],
)
def test_condition_contract(op, expected, operand):
    policy = {"rules": [rule(op, expected)]}
    assert evaluate(policy, "R1", "send", {"value": operand})["decision"] == "allow"
    assert evaluate(policy, "R2", "send", {"value": operand})["decision"] == "pending"
    assert evaluate(policy, "R1", "unknown", {"value": operand})["decision"] == "deny"


def test_precedence_malformed_operands_and_domain_boundaries():
    allow = {"id": "allow", "action_name": "send", "outcome": "allow", "conditions": []}
    deny = rule("gt", 100, outcome="deny")
    policy = {"rules": [allow, deny]}
    assert evaluate(policy, "R1", "send", {"value": 101})["decision"] == "deny"
    assert evaluate(policy, "R1", "send", {"value": "101"})["decision"] == "deny"
    assert evaluate(policy, "R1", "send", {})["decision"] == "deny"
    review = {**deny, "id": "review", "outcome": "review"}
    assert (
        evaluate({"rules": [allow, review]}, "R1", "send", {"value": 101})["decision"] == "pending"
    )
    domain = {"rules": [rule("recipient_domain_in", ["example.test"])]}
    for value in [
        "person@badexample.test",
        "person@example.test.evil",
        "Name <person@example.test>",
        [],
        ["p@example.test", "p@evil.test"],
    ]:
        assert evaluate(domain, "R1", "send", {"value": value})["decision"] == "deny"
    assert (
        evaluate(domain, "R1", "send", {"value": "person@sub.example.test"})["decision"] == "deny"
    )
    domain["rules"][0]["conditions"][0]["include_subdomains"] = True
    assert (
        evaluate(domain, "R1", "send", {"value": "person@sub.example.test"})["decision"] == "allow"
    )
    with pytest.raises(ValueError):
        normalize(
            {"rules": [{**allow, "conditions": [{"path": "/x", "op": "script", "value": "x"}]}]},
            "R1",
        )


@pytest.mark.asyncio
async def test_operator_drafts_admin_publishes_and_policy_change_revokes_tokens(governed):
    headers = {"authorization": "Bearer test-admin"}
    identity = governed.a.workflow.id
    policy = {
        "models": ["*"],
        "outage": {"mode": "open"},
        "rules": [{"id": "send", "action_name": "send", "outcome": "allow", "conditions": []}],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin", headers=headers
    ) as admin:
        admin_app.dependency_overrides[require_admin] = lambda: Principal(
            "operator", client_ids=frozenset([governed.a.client.id])
        )
        try:
            draft = await admin.put(
                f"/admin/workflows/{identity}/policy",
                json={
                    "expected_active_version": 0,
                    "risk_class": "R1",
                    "policy": policy,
                    "reason": "Prepare policy",
                },
            )
            assert draft.status_code == 201, draft.text
            version = draft.json()["version"]
            activate = f"/admin/workflows/{identity}/policy-revisions/{version}/activate"
            assert (
                await admin.post(activate, json={"expected_active_version": 0, "reason": "Publish"})
            ).status_code == 403
            preview = await admin.post(
                "/admin/policies/preview",
                json={
                    "workflow_id": str(identity),
                    "risk_class": "R1",
                    "policy": policy,
                    "action_name": "send",
                    "action": {"value": 7},
                },
            )
            assert preview.json()["decision"] == "allow"
        finally:
            admin_app.dependency_overrides.pop(require_admin)
        assert (
            await admin.post(activate, json={"expected_active_version": 0, "reason": "Reviewed"})
        ).status_code == 200
        auth = {"authorization": "Bearer " + governed.a.key}
        request = {"action_name": "send", "action": {"value": 7}, "summary": "Send"}
        result = (await governed.http.post("/v1/actions/check", headers=auth, json=request)).json()
        assert result["decision"] == preview.json()["decision"]
        assert result["policy_version"] == version and result["matched_rules"] == ["send"]
        second = await admin.put(
            f"/admin/workflows/{identity}/policy",
            json={
                "expected_active_version": version,
                "risk_class": "R1",
                "policy": {"models": ["*"], "rules": []},
                "reason": "Deny send",
            },
        )
        assert second.status_code == 201, second.text
        next_version = second.json()["version"]
        consume = {"token": result["token"], "action_name": "send", "action": {"value": 7}}
        responses = await asyncio.gather(
            admin.post(
                f"/admin/workflows/{identity}/policy-revisions/{next_version}/activate",
                json={"expected_active_version": version, "reason": "Activate deny"},
            ),
            governed.http.post("/v1/actions/consume", headers=auth, json=consume),
        )
        assert responses[0].status_code == 200
        assert responses[1].status_code in (200, 409)
        assert (
            await governed.http.post("/v1/actions/consume", headers=auth, json=consume)
        ).status_code == 409
        # Rollback creates a new revision and does not reuse the original number.
        rolled = await admin.post(
            f"/admin/workflows/{identity}/policy-revisions/{version}/rollback",
            json={"expected_active_version": next_version, "reason": "Restore reviewed policy"},
        )
        assert rolled.status_code == 200 and rolled.json()["active_version"] > next_version
        assert (
            await admin.post(
                activate, json={"expected_active_version": version, "reason": "Stale edit"}
            )
        ).status_code == 409
    async with governed.factory() as session:
        assert len((await session.execute(select(PolicyRevision))).scalars().all()) == 3
        assert (await session.execute(select(AdministrativeEvent))).scalars().all()


def test_signing_rotation_legacy_tokens_and_compact_callbacks(monkeypatch):
    import time

    old = b"o" * 32
    new = b"n" * 32
    monkeypatch.setenv("DECISION_SIGNING_KEY", base64.b64encode(old).decode())
    legacy_payload = {"purpose": "execute", "exp": int(time.time()) + 60}
    raw = b64(json.dumps(legacy_payload).encode())
    legacy = raw + "." + b64(hmac.digest(old, raw.encode(), hashlib.sha256))
    monkeypatch.setenv("SIGNING_KEYS", json.dumps({"0001": base64.b64encode(new).decode()}))
    monkeypatch.setenv("ACTIVE_SIGNING_KEY_ID", "0001")
    assert verify(legacy, "execute")["purpose"] == "execute"
    current = sign(legacy_payload)
    assert verify(current, "execute")["kid"] == "0001"
    import uuid

    assert len(callback(uuid.uuid4(), "a", "123")) <= 64
    monkeypatch.delenv("DECISION_SIGNING_KEY")
    with pytest.raises(GatewayError):
        verify(legacy, "execute")


@pytest.mark.asyncio
async def test_concurrent_human_decisions_refresh_preloaded_approval(governed):
    import uuid
    from datetime import UTC, datetime, timedelta

    from approvals.service import Decision, decide
    from core.models import Approval

    identity = uuid.uuid4()
    async with governed.factory() as session:
        session.add(
            Approval(
                id=identity,
                workflow_id=governed.a.workflow.id,
                action_name="send",
                action_hash=b"x" * 32,
                action_summary="Review",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()
    async with governed.factory() as first, governed.factory() as second:
        # Both requests may have loaded the record while checking reviewer scope.
        records = [await s.get(Approval, identity) for s in (first, second)]
        assert all(r.status == "pending" for r in records)
        results = await asyncio.gather(
            decide(first, identity, Decision(decision="approve", approver="first")),
            decide(second, identity, Decision(decision="deny", approver="second")),
            return_exceptions=True,
        )
        assert sum(isinstance(r, dict) for r in results) == 1
        rejected = next(r for r in results if isinstance(r, GatewayError))
        assert rejected.code == "approval_not_pending"


@pytest.mark.asyncio
async def test_publication_races_delivery_expiry_and_rejects_every_old_decision_channel(
    governed, review_team, monkeypatch
):
    import time
    import uuid
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlparse

    from sqlalchemy import update

    from approvals.channels import signature
    from approvals.expiry import expire_pending
    from approvals.notifications import dispatch, review_link
    from core.models import Approval, Notification, Workflow

    team = review_team
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "mock-hook")
    destinations = {}
    for channel in ("email", "telegram", "webhook"):
        response = await team.admin.post(
            "/admin/integration-destinations",
            headers=team.headers,
            json={
                "name": channel,
                "channel": channel,
                "client_id": team.client_id,
                "group_id": team.group_id,
                "enabled": True,
                "url": "https://hook.test/approval" if channel == "webhook" else None,
                "service_identity": "policy-test" if channel == "webhook" else None,
            },
        )
        assert response.status_code == 201, response.text
        destinations[channel] = response.json()
    command = (await team.admin.post("/admin/telegram/link", headers=team.actor)).json()["command"]
    linked = await team.admin.post(
        "/integrations/telegram",
        headers={"x-telegram-bot-api-secret-token": "mock-hook"},
        json={"message": {"from": {"id": 42}, "chat": {"id": 42}, "text": command}},
    )
    assert linked.status_code == 200, linked.text
    async with governed.factory() as session:
        workflow = await session.get(Workflow, governed.a.workflow.id)
        workflow.policy = {"actions": {"send": {}}, "approval_channels": list(destinations)}
        workflow.risk_class = "R2"
        await session.commit()
    auth = {"authorization": "Bearer " + governed.a.key}
    identities = []
    for _ in range(2):
        response = await governed.http.post(
            "/v1/actions/check",
            headers=auth,
            json={"action_name": "send", "action": {}, "summary": "Review"},
        )
        identities.append(uuid.UUID(response.json()["approval_id"]))
    async with governed.factory() as session:
        record = await session.get(Approval, identities[0])
        token = parse_qs(urlparse(review_link(record, "email")).query)["token"][0]
        await session.execute(
            update(Approval)
            .where(Approval.id == identities[1])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    workflow_id = governed.a.workflow.id
    draft = await team.admin.put(
        f"/admin/workflows/{workflow_id}/policy",
        headers=team.headers,
        json={"expected_active_version": 0, "risk_class": "R2", "policy": {}, "reason": "Deny"},
    )
    assert draft.status_code == 201, draft.text

    async def delivered(*args):
        await asyncio.sleep(0)

    monkeypatch.setattr("approvals.notifications.telegram", delivered)
    monkeypatch.setattr("approvals.channels.email", delivered)
    monkeypatch.setattr("approvals.channels.webhook", delivered)

    async def worker(operation):
        async with governed.factory() as session:
            return await operation(session)

    async with asyncio.timeout(15):
        results = await asyncio.gather(
            team.admin.post(
                f"/admin/workflows/{workflow_id}/policy-revisions/1/activate",
                headers=team.headers,
                json={"expected_active_version": 0, "reason": "Publish"},
            ),
            worker(dispatch),
            worker(expire_pending),
            team.admin.post(
                f"/admin/actions/{identities[0]}/decision",
                headers=team.actor,
                json={"decision": "approve", "approver": "ignored"},
            ),
        )
    assert results[0].status_code == 200
    assert results[3].status_code in (200, 409)
    stale = await governed.http.get(f"/v1/actions/{identities[0]}", headers=auth)
    assert stale.json()["reason"] == "policy_changed"
    requests = [
        team.admin.post(
            f"/admin/actions/{identities[0]}/decision",
            headers=team.actor,
            json={"decision": "approve", "approver": "ignored"},
        ),
        team.admin.post(
            f"/review/{identities[0]}",
            headers=team.actor,
            data={"token": token, "decision": "approve"},
        ),
        team.admin.post(
            "/integrations/telegram",
            headers={"x-telegram-bot-api-secret-token": "mock-hook"},
            json={
                "callback_query": {
                    "from": {"id": 42},
                    "message": {"chat": {"id": 42}},
                    "data": callback(identities[0], "a", "42"),
                }
            },
        ),
    ]
    destination = destinations["webhook"]
    raw = json.dumps(
        {"event_id": str(uuid.uuid4()), "approval_id": str(identities[0]), "decision": "approve"}
    ).encode()
    timestamp = str(int(time.time()))
    requests.append(
        team.admin.post(
            f"/integrations/webhook/{destination['id']}/decision",
            content=raw,
            headers={
                "authorization": "Bearer " + destination["credential"],
                "x-gas-timestamp": timestamp,
                "x-gas-signature": signature(timestamp, raw, destination["credential"].encode()),
            },
        )
    )
    for response in await asyncio.gather(*requests):
        assert response.status_code == 409, response.text
        assert "policy_changed" in response.text
    async with governed.factory() as session:
        assert all(r.invalidated_at for r in (await session.execute(select(Approval))).scalars())
        assert all(
            n.status in ("sent", "cancelled")
            for n in (await session.execute(select(Notification))).scalars()
        )
