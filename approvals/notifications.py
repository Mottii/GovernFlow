import asyncio
import hashlib
import hmac
import json
import os
import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.reviewers import destination_matches, recipients
from approvals.service import content
from approvals.signing import b64, secret, sign
from core.models import (
    Alert,
    Approval,
    ExternalLoginIdentity,
    IntegrationDestination,
    Notification,
    Workflow,
)
from core.security import decrypt_provider_key
from gateway.errors import GatewayError


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
        os.environ.get(
            "PUBLIC_DASHBOARD_URL", os.environ.get("PUBLIC_ADMIN_URL", "http://localhost:3000")
        ).rstrip("/")
        + f"/review/{record.id}?token={token}"
    )


async def telegram(record: Approval, action: dict[str, Any], chats: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
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
                    Notification.status == "pending",
                    Notification.next_attempt <= func.clock_timestamp(),
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
        workflow = await session.get(Workflow, approval.workflow_id)
        if workflow is not None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock_shared(hashtext(:key))"),
                {"key": "agency-permissions:" + str(workflow.agency_id)},
            )
        destination = (
            await session.get(IntegrationDestination, item.destination_id)
            if item.destination_id
            else None
        )
        if (
            workflow is None
            or destination is None
            or not await destination_matches(session, destination, workflow)
        ):
            item.status = "cancelled"
            continue
        client_id = workflow.client_id
        try:
            from approvals.channels import email, webhook

            action = content(approval, client_id)
            if destination.channel == "webhook":
                if destination.secret_ciphertext is None or destination.secret_nonce is None:
                    raise RuntimeError("Destination credential is missing")
                key = decrypt_provider_key(
                    destination.secret_ciphertext,
                    destination.secret_nonce,
                    destination.agency_id,
                    "integration",
                    str(destination.id),
                )
                await webhook(approval, action, destination.config["url"], key.encode())
            else:
                actors = await recipients(session, destination, workflow)
                if destination.channel == "email":
                    for actor in actors:
                        await email(approval, action, actor.email)
                    delivered = bool(actors)
                else:
                    user_ids = [a.user_id for a in actors]
                    chats = list(
                        (
                            await session.execute(
                                select(ExternalLoginIdentity.subject).where(
                                    ExternalLoginIdentity.issuer == "telegram",
                                    ExternalLoginIdentity.user_id.in_(user_ids),
                                )
                            )
                        ).scalars()
                    )
                    delivered = bool(chats)
                    if chats:
                        await telegram(approval, action, chats)
                if not delivered:
                    item.status = "cancelled"
                    continue
            item.status = "sent"
            sent += 1
        except (
            httpx.HTTPError,
            RuntimeError,
            smtplib.SMTPException,
            OSError,
            GatewayError,
            KeyError,
            ValueError,
        ):
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
    from core.database import agency_sessions

    async for session in agency_sessions():
        print(f"Sent {await dispatch(session)} approval notifications")


if __name__ == "__main__":
    asyncio.run(main())
