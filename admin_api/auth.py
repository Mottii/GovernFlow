import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.models import (
    DEFAULT_AGENCY_ID,
    AdminSession,
    Agency,
    AgencyMembership,
    Client,
    ClientAssignment,
    User,
    Workflow,
)

DbSession = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True)
class Principal:
    role: str
    client_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    email: str = "bootstrap admin"
    agency_id: uuid.UUID = DEFAULT_AGENCY_ID
    client_ids: frozenset[uuid.UUID] | None = None
    platform_maintenance: bool = False


def password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def require_admin(
    session: DbSession, authorization: str | None = Header(default=None)
) -> Principal:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not supplied:
        raise HTTPException(401, "Authentication is required")
    expected = os.environ.get("ADMIN_TOKEN", "")
    if expected and hmac.compare_digest(supplied.encode(), expected.encode()):
        return Principal("admin", platform_maintenance=True)
    row = (
        await session.execute(
            select(User, AgencyMembership)
            .join(AdminSession, AdminSession.user_id == User.id)
            .join(
                AgencyMembership,
                (AgencyMembership.user_id == User.id)
                & (AgencyMembership.agency_id == AdminSession.agency_id),
            )
            .join(Agency, Agency.id == AgencyMembership.agency_id)
            .where(
                AdminSession.token_hash == token_hash(supplied),
                AdminSession.expires_at > datetime.now(UTC),
                User.active.is_(True),
                AgencyMembership.active.is_(True),
                Agency.status == "active",
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(401, "Invalid or expired session")
    user, membership = row
    grants = (
        None
        if membership.all_clients
        else frozenset(
            (
                await session.execute(
                    select(ClientAssignment.client_id).where(
                        ClientAssignment.agency_id == membership.agency_id,
                        ClientAssignment.user_id == user.id,
                    )
                )
            ).scalars()
        )
    )
    return Principal(
        membership.role,
        next(iter(grants)) if grants is not None and len(grants) == 1 else None,
        user.id,
        user.email,
        membership.agency_id,
        grants,
    )


Actor = Annotated[Principal, Depends(require_admin)]


def permit(actor: Principal, *roles: str) -> None:
    if actor.role not in roles:
        raise HTTPException(403, "This role cannot perform this action")


def permitted_clients(actor: Principal) -> Any:
    query = select(Client.id).where(Client.agency_id == actor.agency_id)
    if actor.client_ids is not None:
        query = query.where(Client.id.in_(actor.client_ids))
    return query


async def client_scope(session: AsyncSession, actor: Principal, client_id: uuid.UUID) -> None:
    found = (
        await session.execute(permitted_clients(actor).where(Client.id == client_id))
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(404, "Resource not found")


async def workflow_scope(
    session: AsyncSession, actor: Principal, workflow_id: uuid.UUID
) -> Workflow:
    workflow = (
        await session.execute(
            select(Workflow).where(
                Workflow.id == workflow_id,
                Workflow.client_id.in_(permitted_clients(actor)),
            )
        )
    ).scalar_one_or_none()
    if workflow is None:
        raise HTTPException(404, "Workflow not found")
    return workflow


def platform_only(actor: Principal) -> None:
    if not actor.platform_maintenance:
        raise HTTPException(403, "Platform maintenance authentication is required")
