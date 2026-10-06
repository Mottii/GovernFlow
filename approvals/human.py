import hmac
import html
import json
import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.notifications import callback, chat_ids
from approvals.service import Decision, content, decide
from approvals.signing import verify
from core.database import get_session
from core.models import Approval, Workflow
from gateway.errors import GatewayError

router = APIRouter()
Db = Annotated[AsyncSession, Depends(get_session)]


@router.post("/integrations/telegram")
async def telegram_decision(request: Request, session: Db) -> dict[str, Any]:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise GatewayError(401, "unauthorized_callback", "Webhook authentication is required")
    try:
        query = (await request.json())["callback_query"]
        chat = str(query["message"]["chat"]["id"])
        if chat not in chat_ids() or str(query["from"]["id"]) != chat:
            raise ValueError("approver")
        identity, choice, _ = query["data"].split(":")
        approval_id = uuid.UUID(identity)
        if choice not in ("a", "d") or not hmac.compare_digest(
            callback(approval_id, choice, chat), query["data"]
        ):
            raise ValueError("signature")
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(403, "invalid_callback", "Approval callback is invalid") from exc
    await decide(
        session,
        approval_id,
        Decision(decision="approve" if choice == "a" else "deny", approver="telegram:" + chat),
    )
    if query.get("id"):
        return {
            "method": "answerCallbackQuery",
            "callback_query_id": query["id"],
            "text": "Decision recorded",
        }
    return {"ok": True}


@router.get("/review/{approval_id}", response_class=HTMLResponse)
async def review(approval_id: uuid.UUID, token: str, session: Db) -> HTMLResponse:
    payload = verify(token, "review")
    if payload["approval_id"] != str(approval_id):
        raise GatewayError(403, "invalid_decision_token", "Approval binding is invalid")
    record = await session.get(Approval, approval_id)
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    client_id = (
        await session.execute(select(Workflow.client_id).where(Workflow.id == record.workflow_id))
    ).scalar_one()
    details = html.escape(json.dumps(content(record, client_id), ensure_ascii=False, indent=2))
    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>Review action</title></head>
<body><h1>{html.escape(record.action_name)}</h1><p>{html.escape(record.action_summary)}</p>
<p>Status: {record.status}</p><pre>{details}</pre><form method="post">
<input type="hidden" name="token" value="{html.escape(token, quote=True)}">
<button name="decision" value="approve">Approve</button>
<button name="decision" value="deny">Deny</button></form></body></html>'''
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; "
            "frame-ancestors 'none'",
        },
    )


@router.post("/review/{approval_id}")
async def review_decision(approval_id: uuid.UUID, request: Request, session: Db) -> dict[str, Any]:
    from urllib.parse import parse_qs

    fields = parse_qs((await request.body()).decode())
    token = fields.get("token", [""])[0]
    payload = verify(token, "review")
    if payload["approval_id"] != str(approval_id):
        raise GatewayError(403, "invalid_decision_token", "Approval binding is invalid")
    choice = fields.get("decision", [""])[0]
    if choice not in ("approve", "deny"):
        raise GatewayError(400, "invalid_decision", "Decision must be approve or deny")
    return await decide(
        session,
        approval_id,
        Decision(
            decision="approve" if choice == "approve" else "deny", approver=payload["channel"]
        ),
    )
