import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.models import AdminSession, User, Workflow

DbSession = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True)
class Principal:
    role: str
    client_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    email: str = "bootstrap admin"


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
        return Principal("admin")
    user = (
        await session.execute(
            select(User)
            .join(AdminSession, AdminSession.user_id == User.id)
            .where(
                AdminSession.token_hash == token_hash(supplied),
                AdminSession.expires_at > datetime.now(UTC),
                User.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "Invalid or expired session")
    return Principal(user.role, user.client_id, user.id, user.email)


Actor = Annotated[Principal, Depends(require_admin)]


def permit(actor: Principal, *roles: str) -> None:
    if actor.role not in roles:
        raise HTTPException(403, "This role cannot perform this action")


def client_scope(actor: Principal, client_id: uuid.UUID) -> None:
    if actor.client_id is not None and actor.client_id != client_id:
        raise HTTPException(404, "Resource not found")


async def workflow_scope(
    session: AsyncSession, actor: Principal, workflow_id: uuid.UUID
) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, "Workflow not found")
    client_scope(actor, workflow.client_id)
    return workflow
