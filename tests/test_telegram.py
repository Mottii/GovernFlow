import httpx
import pytest
import respx
from sqlalchemy import select, update

from admin_api.app import app as admin_app
from approvals.notifications import dispatch, review_link
from core.models import Approval, Workflow


@pytest.mark.asyncio
async def test_telegram_notification_review_callback_and_replay(governed, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-bot")
    monkeypatch.setenv("TELEGRAM_APPROVER_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "mock-hook")
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(risk_class="R2", policy={"actions": {"send": {}}})
        )
        await session.commit()
    result = (
        await governed.http.post(
            "/v1/actions/check",
            json={
                "action_name": "send",
                "action": {"to": "person@example.test", "text": "<script>bad</script>"},
                "summary": "Send email",
            },
            headers={"authorization": "Bearer " + governed.a.key},
        )
    ).json()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.telegram.org/botmock-bot/sendMessage").respond(
            200, json={"ok": True}
        )
        async with governed.factory() as session:
            assert await dispatch(session) == 1
            assert await dispatch(session) == 0
            approval = (await session.execute(select(Approval))).scalar_one()
            link = review_link(approval, "telegram:42")
        message = route.calls[0].request.content.decode()
        assert "person@example.test" in message and "Send email" in message
        import json

        callback = json.loads(message)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert len(callback.encode()) <= 64
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        assert (await admin.get(link)).status_code == 401
        page = await admin.get(link, headers={"authorization": "Bearer test-admin"})
        assert page.status_code == 200 and "&lt;script&gt;" in page.text
        assert "<script>bad</script>" not in page.text
        update_body = {
            "callback_query": {
                "from": {"id": 42},
                "message": {"chat": {"id": 42}},
                "data": callback,
            }
        }
        assert (await admin.post("/integrations/telegram", json=update_body)).status_code == 401
        headers = {"x-telegram-bot-api-secret-token": "mock-hook"}
        assert (
            await admin.post("/integrations/telegram", json=update_body, headers=headers)
        ).status_code == 200
        assert (
            await admin.post("/integrations/telegram", json=update_body, headers=headers)
        ).status_code == 409
    assert (
        await governed.http.get(
            "/v1/actions/" + result["approval_id"],
            headers={"authorization": "Bearer " + governed.a.key},
        )
    ).json()["decision"] == "allow"
