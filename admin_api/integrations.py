import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from admin_api.auth import Actor, DbSession, client_scope, permit, workflow_scope
from core.models import (
    AgencyMembership,
    ExternalLoginIdentity,
    IntegrationDestination,
    ReviewerAssignment,
    ReviewerGroup,
    ReviewerGroupMember,
    TelegramLinkChallenge,
)
from core.security import encrypt_provider_key

router = APIRouter()


class GroupInput(BaseModel):
    client_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    active: bool = True


class AssignmentInput(BaseModel):
    workflow_ids: list[uuid.UUID | None] = Field(default=[None], max_length=1000)


class DestinationInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    channel: Literal["telegram", "email", "webhook"]
    client_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    enabled: bool = False
    url: str | None = Field(default=None, max_length=2048)
    service_identity: str | None = Field(default=None, min_length=1, max_length=200)


def destination_view(record: IntegrationDestination) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "name": record.name,
        "channel": record.channel,
        "client_id": str(record.client_id) if record.client_id else None,
        "workflow_id": str(record.workflow_id) if record.workflow_id else None,
        "group_id": str(record.group_id) if record.group_id else None,
        "enabled": record.enabled,
        "url": record.config.get("url"),
        "service_identity": record.service_identity,
        "legacy_source": record.legacy_source,
    }


async def group_scope(session: DbSession, actor: Actor, group_id: uuid.UUID) -> ReviewerGroup:
    group = (
        await session.execute(
            select(ReviewerGroup).where(
                ReviewerGroup.id == group_id,
                ReviewerGroup.agency_id == actor.agency_id,
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Reviewer group not found")
    await client_scope(session, actor, group.client_id)
    return group


async def save_group(
    body: GroupInput, session: DbSession, actor: Actor, group: ReviewerGroup | None = None
) -> dict[str, Any]:
    permit(actor, "admin")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    await client_scope(session, actor, body.client_id)
    if group and group.client_id != body.client_id:
        raise HTTPException(400, "A reviewer group cannot change clients")
    for user_id in set(body.user_ids):
        member = await session.get(AgencyMembership, (actor.agency_id, user_id))
        if member is None or member.role not in ("admin", "approver") or not member.active:
            raise HTTPException(
                400, "Every reviewer must be an active agency administrator or approver"
            )
    group = group or ReviewerGroup(
        id=uuid.uuid4(), agency_id=actor.agency_id, client_id=body.client_id
    )
    group.name, group.active = body.name, body.active
    session.add(group)
    await session.flush()
    await session.execute(
        delete(ReviewerGroupMember).where(
            ReviewerGroupMember.group_id == group.id,
            ReviewerGroupMember.agency_id == actor.agency_id,
        )
    )
    session.add_all(
        [
            ReviewerGroupMember(agency_id=actor.agency_id, group_id=group.id, user_id=u)
            for u in set(body.user_ids)
        ]
    )
    await session.commit()
    return {"id": str(group.id), **body.model_dump(mode="json")}


@router.post("/admin/reviewer-groups", status_code=201)
async def create_group(body: GroupInput, session: DbSession, actor: Actor) -> dict[str, Any]:
    try:
        return await save_group(body, session, actor)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Reviewer group already exists") from exc


@router.put("/admin/reviewer-groups/{group_id}")
async def update_group(
    group_id: uuid.UUID, body: GroupInput, session: DbSession, actor: Actor
) -> dict[str, Any]:
    try:
        return await save_group(body, session, actor, await group_scope(session, actor, group_id))
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Reviewer group already exists") from exc


@router.get("/admin/reviewer-groups")
async def groups(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    permit(actor, "admin")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    rows = (
        await session.execute(
            select(ReviewerGroup).where(ReviewerGroup.agency_id == actor.agency_id)
        )
    ).scalars()
    result = []
    for group in rows:
        members = (
            await session.execute(
                select(ReviewerGroupMember.user_id).where(ReviewerGroupMember.group_id == group.id)
            )
        ).scalars()
        scopes = (
            await session.execute(
                select(ReviewerAssignment.workflow_id).where(
                    ReviewerAssignment.group_id == group.id
                )
            )
        ).scalars()
        result.append(
            {
                "id": str(group.id),
                "client_id": str(group.client_id),
                "name": group.name,
                "active": group.active,
                "user_ids": [str(u) for u in members],
                "workflow_ids": [str(w) if w else None for w in scopes],
            }
        )
    return result


@router.put("/admin/reviewer-groups/{group_id}/assignments")
async def assignments(
    group_id: uuid.UUID, body: AssignmentInput, session: DbSession, actor: Actor
) -> dict[str, bool]:
    permit(actor, "admin")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    group = await group_scope(session, actor, group_id)
    for workflow_id in body.workflow_ids:
        if workflow_id:
            workflow = await workflow_scope(session, actor, workflow_id)
            if workflow.client_id != group.client_id:
                raise HTTPException(404, "Workflow not found")
    await session.execute(
        delete(ReviewerAssignment).where(
            ReviewerAssignment.group_id == group.id, ReviewerAssignment.agency_id == actor.agency_id
        )
    )
    session.add_all(
        [
            ReviewerAssignment(
                agency_id=actor.agency_id,
                client_id=group.client_id,
                group_id=group.id,
                workflow_id=w,
            )
            for w in set(body.workflow_ids)
        ]
    )
    await session.commit()
    return {"saved": True}


async def save_destination(
    body: DestinationInput,
    session: DbSession,
    actor: Actor,
    record: IntegrationDestination | None = None,
) -> dict[str, Any]:
    permit(actor, "admin")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    if record and record.client_id is not None and body.client_id != record.client_id:
        raise HTTPException(400, "Create a new destination to change its client")
    if body.client_id:
        await client_scope(session, actor, body.client_id)
    if body.workflow_id:
        workflow = await workflow_scope(session, actor, body.workflow_id)
        if workflow.client_id != body.client_id:
            raise HTTPException(404, "Workflow not found")
    if body.group_id:
        group = await group_scope(session, actor, body.group_id)
        if group.client_id != body.client_id:
            raise HTTPException(404, "Reviewer group not found")
    if body.enabled and (not body.client_id or (body.channel != "webhook" and not body.group_id)):
        raise HTTPException(400, "Assign a client and a reviewer group before enabling delivery")
    if body.url:
        parsed = urlsplit(body.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise HTTPException(
                400, "Webhook URL must be HTTPS without embedded credentials or fragments"
            )
    if body.enabled and body.channel == "webhook" and (not body.url or not body.service_identity):
        raise HTTPException(400, "Webhook URL and service identity are required")
    if record and record.channel != body.channel:
        raise HTTPException(400, "Create a new destination to change its channel")
    record = record or IntegrationDestination(
        id=uuid.uuid4(), agency_id=actor.agency_id, channel=body.channel
    )
    record.name, record.client_id, record.workflow_id = body.name, body.client_id, body.workflow_id
    record.group_id, record.enabled, record.service_identity = (
        body.group_id,
        body.enabled,
        body.service_identity,
    )
    record.config = {"url": body.url} if body.url else {}
    credential = None
    if body.channel == "webhook" and record.credential_hash is None:
        credential = secrets.token_urlsafe(32)
        record.credential_hash = hashlib.sha256(credential.encode()).digest()
        record.secret_ciphertext, record.secret_nonce = encrypt_provider_key(
            credential, actor.agency_id, "integration", str(record.id)
        )
    session.add(record)
    await session.commit()
    result = destination_view(record)
    if credential:
        result["credential"] = credential
    return result


@router.post("/admin/integration-destinations", status_code=201)
async def create_destination(
    body: DestinationInput, session: DbSession, actor: Actor
) -> dict[str, Any]:
    return await save_destination(body, session, actor)


@router.put("/admin/integration-destinations/{destination_id}")
async def update_destination(
    destination_id: uuid.UUID, body: DestinationInput, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    record = (
        await session.execute(
            select(IntegrationDestination).where(
                IntegrationDestination.id == destination_id,
                IntegrationDestination.agency_id == actor.agency_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(404, "Destination not found")
    return await save_destination(body, session, actor, record)


@router.get("/admin/integration-destinations")
async def destinations(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    permit(actor, "admin")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    return [
        destination_view(d)
        for d in (
            await session.execute(
                select(IntegrationDestination).where(
                    IntegrationDestination.agency_id == actor.agency_id,
                )
            )
        ).scalars()
    ]


@router.post("/admin/telegram/link", status_code=201)
async def link_telegram(session: DbSession, actor: Actor) -> dict[str, str]:
    if actor.user_id is None:
        raise HTTPException(403, "Sign in with your own user account to link Telegram")
    token = secrets.token_urlsafe(24)
    session.add(
        TelegramLinkChallenge(
            token_hash=hashlib.sha256(token.encode()).digest(),
            agency_id=actor.agency_id,
            user_id=actor.user_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await session.commit()
    return {"command": "/link " + token, "expires_in": "300"}


@router.delete("/admin/telegram/link", status_code=204)
async def unlink_telegram(session: DbSession, actor: Actor) -> None:
    if actor.user_id is None:
        raise HTTPException(403, "Sign in with your own user account")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "telegram-link:" + str(actor.user_id)},
    )
    chats = (
        await session.execute(
            select(ExternalLoginIdentity.subject).where(
                ExternalLoginIdentity.issuer == "telegram",
                ExternalLoginIdentity.user_id == actor.user_id,
            )
        )
    ).scalars()
    for chat in chats:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "telegram-chat:" + chat}
        )
    await session.execute(
        delete(ExternalLoginIdentity).where(
            ExternalLoginIdentity.issuer == "telegram",
            ExternalLoginIdentity.user_id == actor.user_id,
        )
    )
    await session.commit()
