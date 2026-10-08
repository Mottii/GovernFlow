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

from admin_api.auth import Actor, permit, workflow_scope
from approvals.notifications import callback, chat_ids
from approvals.service import Decision, content, decide
from approvals.signing import verify
from core.database import get_session, set_agency_context
from core.models import DEFAULT_AGENCY_ID, Approval, Client, Workflow
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
    await legacy_approval(session, approval_id)
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
async def review(approval_id: uuid.UUID, token: str, session: Db, actor: Actor) -> HTMLResponse:
    permit(actor, "admin", "approver")
    payload = verify(token, "review")
    if payload["approval_id"] != str(approval_id):
        raise GatewayError(403, "invalid_decision_token", "Approval binding is invalid")
    record = await session.get(Approval, approval_id)
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    workflow = await workflow_scope(session, actor, record.workflow_id)
    client_id = workflow.client_id
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
async def review_decision(
    approval_id: uuid.UUID, request: Request, session: Db, actor: Actor
) -> dict[str, Any]:
    from urllib.parse import parse_qs

    permit(actor, "admin", "approver")
    fields = parse_qs((await request.body()).decode())
    token = fields.get("token", [""])[0]
    payload = verify(token, "review")
    if payload["approval_id"] != str(approval_id):
        raise GatewayError(403, "invalid_decision_token", "Approval binding is invalid")
    record = await session.get(Approval, approval_id)
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    await workflow_scope(session, actor, record.workflow_id)
    choice = fields.get("decision", [""])[0]
    if choice not in ("approve", "deny"):
        raise GatewayError(400, "invalid_decision", "Decision must be approve or deny")
    return await decide(
        session,
        approval_id,
        Decision(decision="approve" if choice == "approve" else "deny", approver=actor.email),
    )


@router.post("/integrations/webhook/decision")
async def webhook_decision(request: Request, session: Db) -> dict[str, Any]:
    from sqlalchemy.exc import IntegrityError

    from approvals.channels import verify_signature
    from core.models import WebhookReceipt

    raw = await request.body()
    verify_signature(
        request.headers.get("x-gas-timestamp", ""), raw, request.headers.get("x-gas-signature", "")
    )
    try:
        payload = json.loads(raw)
        identity = uuid.UUID(payload["event_id"])
        approval_id = uuid.UUID(payload["approval_id"])
        body = Decision.model_validate(payload)
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(400, "invalid_webhook", "Webhook decision is invalid") from exc
    await legacy_approval(session, approval_id)
    session.add(WebhookReceipt(id=identity, agency_id=DEFAULT_AGENCY_ID))
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise GatewayError(409, "webhook_replayed", "Webhook event was already processed") from exc
    return await decide(session, approval_id, body)


async def legacy_approval(session: AsyncSession, approval_id: uuid.UUID) -> Approval:
    """Legacy global integrations never establish authority in a newly provisioned agency."""
    await set_agency_context(session, DEFAULT_AGENCY_ID)
    record = (
        await session.execute(
            select(Approval)
            .join(Workflow, Workflow.id == Approval.workflow_id)
            .join(Client, Client.id == Workflow.client_id)
            .where(Approval.id == approval_id, Client.agency_id == DEFAULT_AGENCY_ID)
        )
    ).scalar_one_or_none()
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    return record
