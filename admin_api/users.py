import asyncio
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from admin_api.auth import Actor, DbSession, password_hash, permit, token_hash
from core.models import AdminSession, LoginBucket, User

router = APIRouter()


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^\S+@\S+\.\S+$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "operator", "approver", "viewer"]
    client_id: uuid.UUID | None = None


class Login(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


@router.post("/admin/users", status_code=201)
async def create_user(body: UserCreate, session: DbSession, actor: Actor) -> dict[str, str]:
    permit(actor, "admin")
    if actor.client_id is not None:
        raise HTTPException(403, "Only a global administrator can manage users")
    if body.role == "admin" and body.client_id is not None:
        raise HTTPException(400, "Administrator accounts must be global")
    salt = os.urandom(16)
    digest = await asyncio.to_thread(password_hash, body.password, salt)
    user = User(
        email=body.email.casefold(),
        password_hash=digest,
        password_salt=salt,
        role=body.role,
        client_id=body.client_id,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "User cannot be created") from exc
    return {"id": str(user.id), "email": user.email, "role": user.role}


@router.get("/admin/users")
async def list_users(session: DbSession, actor: Actor) -> list[dict[str, str | bool | None]]:
    permit(actor, "admin")
    users = (await session.execute(select(User).order_by(User.email))).scalars().all()
    return [
        {
            "id": str(u.id),
            "email": u.email,
            "role": u.role,
            "client_id": str(u.client_id) if u.client_id else None,
            "active": u.active,
        }
        for u in users
    ]


@router.delete("/admin/users/{user_id}", status_code=204)
async def disable_user(user_id: uuid.UUID, session: DbSession, actor: Actor) -> None:
    permit(actor, "admin")
    if actor.user_id == user_id:
        raise HTTPException(400, "Use another administrator to disable your own account")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.active = False
    await session.execute(delete(AdminSession).where(AdminSession.user_id == user_id))
    await session.commit()


@router.post("/admin/auth/login")
async def login(
    body: Login, request: Request, response: Response, session: DbSession
) -> dict[str, str | int]:
    # Separate shared buckets prevent one account or one origin flooding password hashing.
    minute = datetime.now(UTC).replace(second=0, microsecond=0)
    identity = token_hash(body.email.casefold()).hex()
    origin = token_hash(request.client.host if request.client else "unknown").hex()
    for name, cap in (("account:" + identity, 10), ("origin:" + origin, 50)):
        statement = insert(LoginBucket).values(identity=name, minute=minute, count=1)
        allowed = (
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[LoginBucket.identity, LoginBucket.minute],
                    set_={"count": LoginBucket.count + 1},
                    where=LoginBucket.count < cap,
                ).returning(LoginBucket.count)
            )
        ).scalar_one_or_none()
        if allowed is None:
            await session.commit()
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "60"})
    await session.commit()
    user = (
        await session.execute(select(User).where(User.email == body.email.casefold()))
    ).scalar_one_or_none()
    digest = await asyncio.to_thread(
        password_hash, body.password, user.password_salt if user else bytes(16)
    )
    if user is None or not user.active or not hmac.compare_digest(digest, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    token = secrets.token_urlsafe(32)
    session.add(
        AdminSession(
            token_hash=token_hash(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_in": 28800, "role": user.role}


@router.get("/admin/auth/me")
async def me(actor: Actor) -> dict[str, str | None]:
    return {
        "email": actor.email,
        "role": actor.role,
        "client_id": str(actor.client_id) if actor.client_id else None,
    }


@router.post("/admin/auth/logout", status_code=204)
async def logout(
    session: DbSession, actor: Actor, authorization: str | None = Header(default=None)
) -> None:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    await session.execute(
        delete(AdminSession).where(AdminSession.token_hash == token_hash(supplied))
    )
    await session.commit()
