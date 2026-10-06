import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from typing import Any

import httpx

from approvals.notifications import review_link
from core.models import Approval
from gateway.errors import GatewayError


def webhook_secret() -> bytes:
    try:
        value = base64.b64decode(os.environ.get("WEBHOOK_SIGNING_KEY", ""), validate=True)
    except ValueError as exc:
        raise GatewayError(503, "webhook_unavailable", "Webhook signing is not configured") from exc
    if len(value) != 32:
        raise GatewayError(503, "webhook_unavailable", "Webhook signing is not configured")
    return value


def signature(timestamp: str, body: bytes) -> str:
    return hmac.digest(webhook_secret(), timestamp.encode() + b"." + body, hashlib.sha256).hex()


def verify_signature(timestamp: str, body: bytes, supplied: str) -> None:
    try:
        if abs(time.time() - int(timestamp)) > 300 or not hmac.compare_digest(
            signature(timestamp, body), supplied
        ):
            raise ValueError("signature")
    except ValueError as exc:
        raise GatewayError(403, "invalid_webhook", "Webhook signature is invalid or stale") from exc


async def email(record: Approval, action: dict[str, Any]) -> None:
    host = os.environ.get("SMTP_HOST", "")
    recipient = os.environ.get("APPROVAL_EMAIL_TO", "")
    sender = os.environ.get("SMTP_FROM", "")
    if not host or not recipient or not sender:
        raise RuntimeError("SMTP is not configured")
    link = review_link(record, "email:" + recipient)
    message = EmailMessage()
    message["From"], message["To"] = sender, recipient
    message["Subject"] = "Approval required: " + record.action_name
    details = json.dumps(action, ensure_ascii=False, indent=2)
    message.set_content(f"{record.action_summary}\n\n{details}\n\nReview and decide: {link}")
    message.add_alternative(
        f"<h1>{html.escape(record.action_name)}</h1><p>{html.escape(record.action_summary)}</p>"
        f"<pre>{html.escape(details)}</pre><p>"
        f"<a href='{html.escape(link, quote=True)}'>Review action</a></p>",
        subtype="html",
    )

    def send() -> None:
        mode = os.environ.get("SMTP_TLS_MODE", "starttls")
        port = int(os.environ.get("SMTP_PORT", "465" if mode == "ssl" else "587"))
        if mode not in ("ssl", "starttls", "none"):
            raise RuntimeError("SMTP TLS mode is invalid")
        transport = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
        with transport(host, port, timeout=15) as smtp:
            if mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            username = os.environ.get("SMTP_USERNAME")
            if username:
                smtp.login(username, os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(message)

    await asyncio.to_thread(send)


async def webhook(record: Approval, action: dict[str, Any]) -> None:
    url = os.environ.get("APPROVAL_WEBHOOK_URL", "")
    if not url:
        raise RuntimeError("Approval webhook is not configured")
    timestamp = str(int(time.time()))
    body = json.dumps(
        {
            "event_id": str(uuid.uuid4()),
            "type": "action_proposed",
            "approval_id": str(record.id),
            "workflow_id": str(record.workflow_id),
            "action_name": record.action_name,
            "summary": record.action_summary,
            "action": action,
            "expires_at": record.expires_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(
            url,
            content=body,
            headers={
                "content-type": "application/json",
                "x-gas-timestamp": timestamp,
                "x-gas-signature": signature(timestamp, body),
            },
        )
        response.raise_for_status()
