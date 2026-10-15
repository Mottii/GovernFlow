"""One authorization path for dashboard, email, Telegram, and notification recipients."""

import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.auth import Principal, permit, workflow_scope
from core.models import (
    Agency,
    AgencyMembership,
    ClientAssignment,
    IntegrationDestination,
    ReviewerAssignment,
    ReviewerGroup,
    ReviewerGroupMember,
    User,
    Workflow,
)


async def principal_for_user(
    session: AsyncSession, user_id: uuid.UUID, agency_id: uuid.UUID
) -> Principal:
    row = (
        await session.execute(
            select(User, AgencyMembership)
            .join(AgencyMembership, AgencyMembership.user_id == User.id)
            .join(Agency, Agency.id == AgencyMembership.agency_id)
            .execution_options(populate_existing=True)
            .where(
                User.id == user_id,
                User.active.is_(True),
                AgencyMembership.agency_id == agency_id,
                AgencyMembership.active.is_(True),
                Agency.status == "active",
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(403, "Reviewer access is no longer active")
    user, member = row
    grants = (
        None
        if member.all_clients
        else frozenset(
            (
                await session.execute(
                    select(ClientAssignment.client_id).where(
                        ClientAssignment.agency_id == agency_id, ClientAssignment.user_id == user_id
                    )
                )
            ).scalars()
        )
    )
    return Principal(
        member.role, user_id=user_id, email=user.email, agency_id=agency_id, client_ids=grants
    )


async def reviewer_groups(
    session: AsyncSession, actor: Principal, workflow: Workflow
) -> set[uuid.UUID]:
    if actor.user_id is None:
        return set()
    return set(
        (
            await session.execute(
                select(ReviewerGroup.id)
                .join(
                    ReviewerGroupMember,
                    (ReviewerGroupMember.group_id == ReviewerGroup.id)
                    & (ReviewerGroupMember.agency_id == ReviewerGroup.agency_id),
                )
                .join(ReviewerAssignment, ReviewerAssignment.group_id == ReviewerGroup.id)
                .where(
                    ReviewerGroup.agency_id == actor.agency_id,
                    ReviewerGroup.client_id == workflow.client_id,
                    ReviewerGroup.active.is_(True),
                    ReviewerGroupMember.user_id == actor.user_id,
                    or_(
                        ReviewerAssignment.workflow_id.is_(None),
                        ReviewerAssignment.workflow_id == workflow.id,
                    ),
                )
            )
        ).scalars()
    )


async def require_reviewer(
    session: AsyncSession, actor: Principal, workflow_id: uuid.UUID
) -> Workflow:
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtext(:key))"),
        {"key": "agency-permissions:" + str(actor.agency_id)},
    )
    if actor.user_id:
        actor = await principal_for_user(session, actor.user_id, actor.agency_id)
    permit(actor, "admin", "approver")
    workflow = await workflow_scope(session, actor, workflow_id)
    # Agency administrators retain dashboard recovery authority during reviewer migration.
    if actor.role != "admin" and not await reviewer_groups(session, actor, workflow):
        raise HTTPException(403, "No current reviewer assignment for this workflow")
    return workflow


async def destination_matches(
    session: AsyncSession, destination: IntegrationDestination, workflow: Workflow
) -> bool:
    if (
        not destination.enabled
        or destination.agency_id != workflow.agency_id
        or (
            destination.client_id != workflow.client_id
            or destination.workflow_id is not None
            and destination.workflow_id != workflow.id
        )
    ):
        return False
    if destination.channel == "webhook":
        return True
    return (
        await session.execute(
            select(ReviewerAssignment.id)
            .join(ReviewerGroup, ReviewerGroup.id == ReviewerAssignment.group_id)
            .where(
                ReviewerGroup.active.is_(True),
                ReviewerGroup.id == destination.group_id,
                ReviewerAssignment.agency_id == workflow.agency_id,
                ReviewerAssignment.client_id == workflow.client_id,
                or_(
                    ReviewerAssignment.workflow_id.is_(None),
                    ReviewerAssignment.workflow_id == workflow.id,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def recipients(
    session: AsyncSession, destination: IntegrationDestination, workflow: Workflow
) -> list[Principal]:
    if not await destination_matches(session, destination, workflow):
        return []
    ids = (
        (
            await session.execute(
                select(ReviewerGroupMember.user_id).where(
                    ReviewerGroupMember.agency_id == workflow.agency_id,
                    ReviewerGroupMember.group_id == destination.group_id,
                )
            )
        )
        .scalars()
        .all()
    )
    result = []
    for user_id in ids:
        try:
            actor = await principal_for_user(session, user_id, workflow.agency_id)
            await require_reviewer(session, actor, workflow.id)
            result.append(actor)
        except HTTPException:
            continue
    return result
