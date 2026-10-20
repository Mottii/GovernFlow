"""Immutable drafts, shared previews and serialized policy activation."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, update

from admin_api.auth import Actor, DbSession, permit, workflow_scope
from audit.administration import record
from core.models import Approval, Notification, PolicyRevision, Workflow
from policy.accounting import database_now
from policy.revisions import digest, evaluate, lock, normalize

router = APIRouter()


class Draft(BaseModel):
    expected_active_version: int = Field(ge=0)
    risk_class: Literal["R0", "R1", "R2", "R3"]
    policy: dict[str, Any]
    reason: str = Field(min_length=1, max_length=200)


class Activation(BaseModel):
    expected_active_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=200)


class Preview(BaseModel):
    workflow_id: uuid.UUID
    risk_class: Literal["R0", "R1", "R2", "R3"]
    policy: dict[str, Any]
    action_name: str = Field(min_length=1, max_length=100)
    action: dict[str, Any]


def validated(policy: dict[str, Any], risk: str) -> dict[str, Any]:
    try:
        return normalize(policy, risk)
    except ValidationError as error:
        raise HTTPException(
            422, error.errors(include_input=False, include_context=False, include_url=False)
        ) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from error


def view(revision: PolicyRevision) -> dict[str, Any]:
    return {
        "id": str(revision.id),
        "version": revision.version,
        "risk_class": revision.risk_class,
        "policy": revision.policy,
        "hash": revision.content_hash.hex(),
        "author": revision.actor,
        "source_version": revision.source_version,
        "legacy_source": revision.legacy_source,
        "created_at": revision.created_at.isoformat(),
    }


async def save(
    session: DbSession,
    actor: Actor,
    workflow: Workflow,
    policy: dict[str, Any],
    risk: str,
    reason: str,
    source: int | None = None,
) -> PolicyRevision:
    version = (
        await session.execute(
            select(func.coalesce(func.max(PolicyRevision.version), 0)).where(
                PolicyRevision.workflow_id == workflow.id
            )
        )
    ).scalar_one() + 1
    item = PolicyRevision(
        agency_id=actor.agency_id,
        client_id=workflow.client_id,
        workflow_id=workflow.id,
        version=version,
        risk_class=risk,
        policy=policy,
        content_hash=digest(policy, risk),
        actor=actor.email,
        source_version=source,
    )
    session.add(item)
    await record(
        session,
        actor.agency_id,
        actor.email,
        "policy",
        workflow.id,
        "draft_created",
        version=version,
        policy_hash=item.content_hash.hex(),
        reason=reason,
    )
    return item


async def publish(
    session: DbSession, actor: Actor, workflow: Workflow, item: PolicyRevision, reason: str
) -> None:
    workflow.policy, workflow.policy_version, workflow.risk_class = (
        item.policy,
        item.version,
        item.risk_class,
    )
    workflow.policy_review_required = False
    now = await database_now(session)
    stale = list(
        (
            await session.execute(
                select(Approval)
                .where(
                    Approval.workflow_id == workflow.id,
                    Approval.consumed_at.is_(None),
                    Approval.invalidated_at.is_(None),
                    Approval.policy_version != item.version,
                )
                .with_for_update()
            )
        ).scalars()
    )
    for approval in stale:
        approval.invalidated_at = now
    if stale:
        await session.execute(
            update(Notification)
            .where(
                Notification.approval_id.in_([a.id for a in stale]),
                Notification.status.in_(("pending", "sending", "retry")),
            )
            .values(status="cancelled")
        )
    await record(
        session,
        actor.agency_id,
        actor.email,
        "policy",
        workflow.id,
        "activated",
        version=item.version,
        policy_hash=item.content_hash.hex(),
        reason=reason,
    )


def precondition(workflow: Workflow, expected: int) -> None:
    if workflow.policy_version != expected:
        raise HTTPException(409, {"active_version": workflow.policy_version})


@router.put("/admin/workflows/{workflow_id}/policy", status_code=201)
async def draft(
    workflow_id: uuid.UUID, body: Draft, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    await workflow_scope(session, actor, workflow_id)
    workflow = await lock(session, workflow_id)
    precondition(workflow, body.expected_active_version)
    policy = validated(body.policy, body.risk_class)
    item = await save(session, actor, workflow, policy, body.risk_class, body.reason)
    await session.commit()
    return view(item)


@router.get("/admin/workflows/{workflow_id}/policy-revisions")
async def history(workflow_id: uuid.UUID, session: DbSession, actor: Actor) -> dict[str, Any]:
    workflow = await workflow_scope(session, actor, workflow_id)
    rows = (
        await session.execute(
            select(PolicyRevision)
            .where(PolicyRevision.workflow_id == workflow_id)
            .order_by(PolicyRevision.version.desc())
            .limit(200)
        )
    ).scalars()
    return {
        "active_version": workflow.policy_version,
        "review_required": workflow.policy_review_required,
        "revisions": [view(r) for r in rows],
    }


@router.post("/admin/workflows/{workflow_id}/policy-revisions/{version}/activate")
async def activate(
    workflow_id: uuid.UUID, version: int, body: Activation, session: DbSession, actor: Actor
) -> dict[str, int]:
    permit(actor, "admin")
    await workflow_scope(session, actor, workflow_id)
    workflow = await lock(session, workflow_id)
    precondition(workflow, body.expected_active_version)
    item = (
        await session.execute(
            select(PolicyRevision).where(
                PolicyRevision.workflow_id == workflow_id, PolicyRevision.version == version
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Revision not found")
    if version <= workflow.policy_version:
        raise HTTPException(409, "Use rollback to publish a new copy of earlier policy")
    await publish(session, actor, workflow, item, body.reason)
    await session.commit()
    return {"active_version": version}


@router.post("/admin/workflows/{workflow_id}/policy-revisions/{version}/rollback")
async def rollback(
    workflow_id: uuid.UUID, version: int, body: Activation, session: DbSession, actor: Actor
) -> dict[str, int]:
    permit(actor, "admin")
    await workflow_scope(session, actor, workflow_id)
    workflow = await lock(session, workflow_id)
    precondition(workflow, body.expected_active_version)
    old = (
        await session.execute(
            select(PolicyRevision).where(
                PolicyRevision.workflow_id == workflow_id, PolicyRevision.version == version
            )
        )
    ).scalar_one_or_none()
    if old is None:
        raise HTTPException(404, "Revision not found")
    item = await save(
        session,
        actor,
        workflow,
        validated(old.policy, old.risk_class),
        old.risk_class,
        body.reason,
        version,
    )
    await publish(session, actor, workflow, item, body.reason)
    await session.commit()
    return {"active_version": item.version}


@router.post("/admin/policies/preview")
async def preview(body: Preview, session: DbSession, actor: Actor) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    workflow = await workflow_scope(session, actor, body.workflow_id)
    policy = validated(body.policy, body.risk_class)
    return {
        "normalized_policy": policy,
        "active_version": workflow.policy_version,
        **evaluate(policy, body.risk_class, body.action_name, body.action),
    }
