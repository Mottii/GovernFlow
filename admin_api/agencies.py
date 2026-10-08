"""Agency provisioning and membership administration."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from admin_api.auth import Actor, DbSession, client_scope, permit, platform_only
from core.models import AdminSession, Agency, AgencyMembership, Client, ClientAssignment, User

router = APIRouter()


class AgencyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MembershipChange(BaseModel):
    role: Literal["admin", "operator", "approver", "viewer"]
    all_clients: bool = False
    client_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    active: bool = True


@router.post("/admin/agencies", status_code=201)
async def create_agency(body: AgencyCreate, session: DbSession, actor: Actor) -> dict[str, str]:
    platform_only(actor)
    agency = Agency(name=body.name)
    session.add(agency)
    await session.commit()
    return {"id": str(agency.id), "name": agency.name, "status": agency.status}


@router.get("/admin/agency")
async def agency(session: DbSession, actor: Actor) -> dict[str, str]:
    record = await session.get(Agency, actor.agency_id)
    if record is None:
        raise HTTPException(404, "Agency not found")
    return {"id": str(record.id), "name": record.name, "status": record.status}


async def save_membership(
    session: DbSession, agency_id: uuid.UUID, user_id: uuid.UUID, body: MembershipChange
) -> dict[str, Any]:
    # Serialize permission administration within an agency to protect the last administrator.
    record = (
        await session.execute(select(Agency).where(Agency.id == agency_id).with_for_update())
    ).scalar_one_or_none()
    if record is None or await session.get(User, user_id) is None:
        raise HTTPException(404, "Resource not found")
    if body.all_clients and body.client_ids:
        raise HTTPException(400, "All-client access cannot be combined with assignments")
    if body.role == "admin" and not body.all_clients:
        raise HTTPException(400, "Administrator accounts require all-client access")
    for client_id in set(body.client_ids):
        found = (
            await session.execute(
                select(Client.id).where(
                    Client.id == client_id,
                    Client.agency_id == agency_id,
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise HTTPException(404, "Client not found")
    membership = await session.get(AgencyMembership, (agency_id, user_id))
    if (
        membership is not None
        and membership.active
        and membership.role == "admin"
        and (not body.active or body.role != "admin")
    ):
        another = (
            await session.execute(
                select(AgencyMembership.user_id)
                .where(
                    AgencyMembership.agency_id == agency_id,
                    AgencyMembership.user_id != user_id,
                    AgencyMembership.role == "admin",
                    AgencyMembership.active.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if another is None:
            raise HTTPException(409, "Keep at least one active agency administrator")
    if membership is None:
        membership = AgencyMembership(agency_id=agency_id, user_id=user_id, role=body.role)
        session.add(membership)
    membership.role, membership.all_clients, membership.active = (
        body.role,
        body.all_clients,
        body.active,
    )
    await session.flush()
    await session.execute(
        delete(ClientAssignment).where(
            ClientAssignment.agency_id == agency_id,
            ClientAssignment.user_id == user_id,
        )
    )
    session.add_all(
        [
            ClientAssignment(agency_id=agency_id, user_id=user_id, client_id=c)
            for c in set(body.client_ids)
        ]
    )
    if not body.active:
        await session.execute(
            delete(AdminSession).where(
                AdminSession.agency_id == agency_id,
                AdminSession.user_id == user_id,
            )
        )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Membership could not be saved") from exc
    return {"user_id": str(user_id), "agency_id": str(agency_id), **body.model_dump(mode="json")}


@router.put("/admin/agencies/{agency_id}/memberships/{user_id}")
async def provision_membership(
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MembershipChange,
    session: DbSession,
    actor: Actor,
) -> dict[str, Any]:
    platform_only(actor)
    return await save_membership(session, agency_id, user_id, body)


@router.put("/admin/memberships/{user_id}")
async def update_membership(
    user_id: uuid.UUID, body: MembershipChange, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin")
    if actor.client_ids is not None:
        raise HTTPException(403, "All-client access is required")
    if await session.get(AgencyMembership, (actor.agency_id, user_id)) is None:
        raise HTTPException(404, "Membership not found")
    for client_id in body.client_ids:
        await client_scope(session, actor, client_id)
    return await save_membership(session, actor.agency_id, user_id, body)
