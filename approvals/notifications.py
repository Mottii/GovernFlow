import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.service import content
from approvals.signing import b64, secret, sign
from core.models import Alert, Approval, Notification, Workflow


def chat_ids() -> list[str]:
    return [
        value.strip()
        for value in os.environ.get("TELEGRAM_APPROVER_CHAT_IDS", "").split(",")
        if value.strip()
    ]


def callback(approval_id: uuid.UUID, choice: str, chat_id: str) -> str:
    message = approval_id.hex + ":" + choice
    signature = b64(hmac.digest(secret(), (message + ":" + chat_id).encode(), hashlib.sha256)[:16])
    return message + ":" + signature


def review_link(record: Approval, channel: str) -> str:
    token = sign(
        {
            "purpose": "review",
            "approval_id": str(record.id),
            "channel": channel,
            "exp": int(record.expires_at.timestamp()),
        }
    )
    return (
        os.environ.get("PUBLIC_ADMIN_URL", "http://localhost:8001").rstrip("/")
        + f"/review/{record.id}?token={token}"
    )


async def telegram(record: Approval, action: dict[str, Any]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chats = chat_ids()
    if not token or not chats:
        raise RuntimeError("Telegram is not configured")
    details = json.dumps(action, ensure_ascii=False, indent=2)
    async with httpx.AsyncClient(timeout=15) as http:
        for chat in chats:
            message = (
                f"Approval required: {record.action_name}\n{record.action_summary[:500]}\n\n"
                f"{details[:1800]}\n\nFull action: {review_link(record, 'telegram:' + chat)}"
            )
            response = await http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": message[:4096],
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Approve",
                                    "callback_data": callback(record.id, "a", chat),
                                },
                                {"text": "Deny", "callback_data": callback(record.id, "d", chat)},
                            ]
                        ]
                    },
                },
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("Telegram rejected notification")


async def dispatch(session: AsyncSession) -> int:
    records = (
        (
            await session.execute(
                select(Notification)
                .where(
                    Notification.status == "pending", Notification.next_attempt <= datetime.now(UTC)
                )
                .with_for_update(skip_locked=True)
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for item in records:
        approval = await session.get(Approval, item.approval_id)
        if (
            approval is None
            or approval.status != "pending"
            or approval.expires_at <= datetime.now(UTC)
        ):
            item.status = "cancelled"
            continue
        client_id = (
            await session.execute(
                select(Workflow.client_id).where(Workflow.id == approval.workflow_id)
            )
        ).scalar_one()
        try:
            if item.channel != "telegram":
                raise RuntimeError("Channel is not configured")
            await telegram(approval, content(approval, client_id))
            item.status = "sent"
            sent += 1
        except (httpx.HTTPError, RuntimeError):
            if item.attempts == 0:
                session.add(
                    Alert(
                        client_id=client_id,
                        workflow_id=approval.workflow_id,
                        kind="notification_failed",
                        details={"approval_id": str(approval.id), "channel": item.channel},
                    )
                )
            item.next_attempt = datetime.now(UTC) + timedelta(
                seconds=min(3600, 30 * 2 ** min(item.attempts, 7))
            )
        item.attempts += 1
    await session.commit()
    return sent


async def main() -> None:
    from core.database import session_factory

    async with session_factory() as session:
        print(f"Sent {await dispatch(session)} approval notifications")


if __name__ == "__main__":
    asyncio.run(main())
