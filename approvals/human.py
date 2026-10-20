import hashlib
import hmac
import html
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.auth import Actor, permit
from approvals.notifications import callback
from approvals.reviewers import (
    destination_matches,
    principal_for_user,
    require_reviewer,
    reviewer_groups,
)
from approvals.service import Decision, content, decide
from approvals.signing import verify
from core.database import agency_rls_enabled, get_session, set_agency_context
from core.models import (
    Approval,
    ExternalLoginIdentity,
    IntegrationDestination,
    TelegramLinkChallenge,
)
from core.security import decrypt_provider_key
from gateway.errors import GatewayError

router = APIRouter()
Db = Annotated[AsyncSession, Depends(get_session)]


async def callback_context(
    session: AsyncSession, kind: str, value: Any, digest: bytes | None = None
) -> None:
    if agency_rls_enabled():
        sql = {
            "link": "SELECT public.governflow_link_agency(:value)",
            "approval": "SELECT public.governflow_approval_agency(:value)",
            "destination": "SELECT public.governflow_destination_agency(:value,:digest)",
        }[kind]
        agency_id = (
            await session.execute(text(sql), {"value": value, "digest": digest})
        ).scalar_one_or_none()
    else:
        query = {
            "link": select(TelegramLinkChallenge.agency_id).where(
                TelegramLinkChallenge.token_hash == value
            ),
            "approval": select(Approval.agency_id).where(Approval.id == value),
            "destination": select(IntegrationDestination.agency_id).where(
                IntegrationDestination.id == value,
                IntegrationDestination.credential_hash == digest,
                IntegrationDestination.enabled.is_(True),
                IntegrationDestination.channel == "webhook",
            ),
        }[kind]
        agency_id = (await session.execute(query)).scalar_one_or_none()
    if agency_id is None:
        raise GatewayError(
            403, "unauthorized_callback", "Callback authority is invalid or disabled"
        )
    await set_agency_context(session, agency_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtext(:key))"),
        {"key": "agency-permissions:" + str(agency_id)},
    )


async def telegram_link(session: AsyncSession, message: dict[str, Any]) -> dict[str, Any]:
    try:
        chat = str(message["chat"]["id"])
        if (
            str(message["from"]["id"]) != chat
            or message["chat"].get("type", "private") != "private"
        ):
            raise ValueError("private chat required")
        command, token = message["text"].split()
        if command != "/link":
            raise ValueError("command")
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(
            403, "invalid_link", "Use your personal linking command in a private chat"
        ) from exc
    digest = hashlib.sha256(token.encode()).digest()
    await callback_context(session, "link", digest)
    challenge = (
        await session.execute(
            select(TelegramLinkChallenge)
            .where(TelegramLinkChallenge.token_hash == digest)
            .with_for_update()
        )
    ).scalar_one()
    if challenge.consumed_at or challenge.expires_at <= datetime.now(UTC):
        raise GatewayError(403, "invalid_link", "Linking command has expired or was already used")
    linking_actor = await principal_for_user(session, challenge.user_id, challenge.agency_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "telegram-link:" + str(challenge.user_id)},
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "telegram-chat:" + chat}
    )
    identity = await session.get(ExternalLoginIdentity, ("telegram", chat))
    if identity and identity.user_id != challenge.user_id:
        raise GatewayError(409, "identity_in_use", "Telegram account is already linked")
    old_chats = (
        await session.execute(
            select(ExternalLoginIdentity.subject).where(
                ExternalLoginIdentity.issuer == "telegram",
                ExternalLoginIdentity.user_id == challenge.user_id,
            )
        )
    ).scalars()
    for old_chat in old_chats:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "telegram-chat:" + old_chat},
        )
    await session.execute(
        delete(ExternalLoginIdentity).where(
            ExternalLoginIdentity.issuer == "telegram",
            ExternalLoginIdentity.user_id == challenge.user_id,
        )
    )
    session.add(ExternalLoginIdentity(issuer="telegram", subject=chat, user_id=challenge.user_id))
    challenge.consumed_at = datetime.now(UTC)
    from audit.administration import record

    await record(
        session,
        challenge.agency_id,
        linking_actor.email,
        "external_identity",
        challenge.user_id,
        "linked",
        changed_fields=["telegram"],
    )
    await session.commit()
    return {"method": "sendMessage", "chat_id": chat, "text": "Telegram account linked"}


@router.post("/integrations/telegram")
async def telegram_decision(request: Request, session: Db) -> dict[str, Any]:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise GatewayError(401, "unauthorized_callback", "Webhook authentication is required")
    payload = await request.json()
    if "message" in payload:
        return await telegram_link(session, payload["message"])
    try:
        query = payload["callback_query"]
        chat = str(query["message"]["chat"]["id"])
        if (
            str(query["from"]["id"]) != chat
            or query["message"]["chat"].get("type", "private") != "private"
        ):
            raise ValueError("approver")
        parts = query["data"].split(":")
        if len(parts) == 4:
            key_id, identity, choice, _ = parts
        elif len(parts) == 3:
            identity, choice, _ = parts
            key_id = "0000"
        else:
            raise ValueError("callback")
        approval_id = uuid.UUID(identity)
        if choice not in ("a", "d") or not hmac.compare_digest(
            callback(approval_id, choice, chat, key_id, legacy=len(parts) == 3), query["data"]
        ):
            raise ValueError("signature")
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(403, "invalid_callback", "Approval callback is invalid") from exc
    await callback_context(session, "approval", approval_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtext(:key))"),
        {"key": "telegram-chat:" + chat},
    )
    linked = await session.get(ExternalLoginIdentity, ("telegram", chat))
    if linked is None:
        raise GatewayError(403, "unlinked_reviewer", "Link your Telegram account before reviewing")
    agency_id = session.info["agency_id"]
    actor = await principal_for_user(session, linked.user_id, agency_id)
    record = await session.get(Approval, approval_id)
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    workflow = await require_reviewer(session, actor, record.workflow_id)
    groups = await reviewer_groups(session, actor, workflow)
    destinations = (
        await session.execute(
            select(IntegrationDestination).where(
                IntegrationDestination.agency_id == agency_id,
                IntegrationDestination.channel == "telegram",
                IntegrationDestination.group_id.in_(groups),
                IntegrationDestination.enabled.is_(True),
            )
        )
    ).scalars()
    if not any([await destination_matches(session, d, workflow) for d in destinations]):
        raise GatewayError(403, "unassigned_reviewer", "No current Telegram destination assignment")
    await decide(
        session,
        approval_id,
        Decision(
            decision="approve" if choice == "a" else "deny",
            approver="telegram:" + str(actor.user_id),
        ),
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
    workflow = await require_reviewer(session, actor, record.workflow_id)
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
    await require_reviewer(session, actor, record.workflow_id)
    choice = fields.get("decision", [""])[0]
    if choice not in ("approve", "deny"):
        raise GatewayError(400, "invalid_decision", "Decision must be approve or deny")
    return await decide(
        session,
        approval_id,
        Decision(decision="approve" if choice == "approve" else "deny", approver=actor.email),
    )


@router.post("/integrations/webhook/decision")
async def legacy_webhook_decision() -> None:
    raise GatewayError(410, "destination_required", "Configure a scoped webhook destination")


@router.post("/integrations/webhook/{destination_id}/decision")
async def webhook_decision(
    destination_id: uuid.UUID, request: Request, session: Db
) -> dict[str, Any]:
    from sqlalchemy.exc import IntegrityError

    from approvals.channels import verify_signature
    from core.models import WebhookReceipt

    credential = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not credential:
        raise GatewayError(401, "unauthorized_callback", "Destination credential is required")
    await callback_context(
        session, "destination", destination_id, hashlib.sha256(credential.encode()).digest()
    )
    destination = await session.get(IntegrationDestination, destination_id)
    assert destination is not None
    if destination.secret_ciphertext is None or destination.secret_nonce is None:
        raise GatewayError(403, "unauthorized_callback", "Destination credential is unavailable")
    key = decrypt_provider_key(
        destination.secret_ciphertext,
        destination.secret_nonce,
        destination.agency_id,
        "integration",
        str(destination.id),
        key_id=destination.encryption_key_id,
    )
    raw = await request.body()
    verify_signature(
        request.headers.get("x-gas-timestamp", ""),
        raw,
        request.headers.get("x-gas-signature", ""),
        key.encode(),
    )
    try:
        payload = json.loads(raw)
        identity = uuid.UUID(payload["event_id"])
        approval_id = uuid.UUID(payload["approval_id"])
        body = Decision.model_validate({**payload, "approver": "webhook:" + str(destination.id)})
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(400, "invalid_webhook", "Webhook decision is invalid") from exc
    record = await session.get(Approval, approval_id)
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    from core.models import Workflow

    workflow = await session.get(Workflow, record.workflow_id)
    if workflow is None or not await destination_matches(session, destination, workflow):
        raise GatewayError(403, "unassigned_destination", "Destination cannot review this workflow")
    body = body.model_copy(
        update={
            "approver": "webhook:"
            + str(destination.id)
            + ":"
            + (destination.service_identity or "")
        }
    )
    session.add(WebhookReceipt(id=identity, agency_id=destination.agency_id))
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise GatewayError(409, "webhook_replayed", "Webhook event was already processed") from exc
    return await decide(session, approval_id, body)
