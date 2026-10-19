import asyncio
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from admin_api.auth import Actor, DbSession, client_scope, password_hash, permit, token_hash
from audit.administration import record as audit_record
from core.database import agency_rls_enabled, set_agency_context
from core.models import AdminSession, Agency, AgencyMembership, ClientAssignment, LoginBucket, User

router = APIRouter()


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^\S+@\S+\.\S+$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "operator", "approver", "viewer"]
    client_id: uuid.UUID | None = None
    client_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)
    all_clients: bool | None = None


class Login(BaseModel):
    agency_id: uuid.UUID | None = None
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


@router.post("/admin/users", status_code=201)
async def create_user(body: UserCreate, session: DbSession, actor: Actor) -> dict[str, str]:
    permit(actor, "admin")
    if actor.client_ids is not None:
        raise HTTPException(403, "An agency administrator with all-client access is required")
    if body.role == "admin" and body.client_id is not None:
        raise HTTPException(400, "Administrator accounts must be global")
    if body.client_id is not None and body.client_ids is not None:
        raise HTTPException(400, "Use client_id or client_ids, not both")
    grants = set(
        body.client_ids
        if body.client_ids is not None
        else ([body.client_id] if body.client_id is not None else [])
    )
    # Preserve the old omitted-client grant. New explicit empty lists grant no client access.
    all_clients = (
        body.all_clients
        if body.all_clients is not None
        else (body.client_id is None and body.client_ids is None)
    )
    if all_clients and grants:
        raise HTTPException(400, "All-client access cannot be combined with assignments")
    if body.role == "admin" and not all_clients:
        raise HTTPException(400, "Administrator accounts require all-client access")
    for client_id in grants:
        await client_scope(session, actor, client_id)
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
        await session.flush()
        session.add(
            AgencyMembership(
                agency_id=actor.agency_id,
                user_id=user.id,
                role=body.role,
                all_clients=all_clients,
            )
        )
        await session.flush()
        session.add_all(
            [
                ClientAssignment(
                    agency_id=actor.agency_id,
                    user_id=user.id,
                    client_id=client_id,
                )
                for client_id in grants
            ]
        )
        await audit_record(
            session,
            actor.agency_id,
            actor.email,
            "user",
            user.id,
            "created",
            role=body.role,
            all_clients=all_clients,
            client_ids=[str(c) for c in grants],
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "User cannot be created") from exc
    return {"id": str(user.id), "email": user.email, "role": user.role}


@router.get("/admin/users")
async def list_users(session: DbSession, actor: Actor) -> list[dict[str, object]]:
    permit(actor, "admin")
    rows = (
        await session.execute(
            select(User, AgencyMembership)
            .join(AgencyMembership, AgencyMembership.user_id == User.id)
            .where(AgencyMembership.agency_id == actor.agency_id)
            .order_by(User.email)
        )
    ).all()
    assignments = (
        (
            await session.execute(
                select(ClientAssignment).where(ClientAssignment.agency_id == actor.agency_id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(u.id),
            "email": u.email,
            "role": membership.role,
            "client_id": next((str(a.client_id) for a in assignments if a.user_id == u.id), None)
            if sum(a.user_id == u.id for a in assignments) == 1
            else None,
            "client_ids": [str(a.client_id) for a in assignments if a.user_id == u.id],
            "all_clients": membership.all_clients,
            "active": u.active and membership.active,
        }
        for u, membership in rows
    ]


@router.delete("/admin/users/{user_id}", status_code=204)
async def disable_user(user_id: uuid.UUID, session: DbSession, actor: Actor) -> None:
    permit(actor, "admin")
    if actor.user_id == user_id:
        raise HTTPException(400, "Use another administrator to disable your own account")
    membership = await session.get(AgencyMembership, (actor.agency_id, user_id))
    if membership is None:
        raise HTTPException(404, "User not found")
    from admin_api.agencies import MembershipChange, save_membership

    await save_membership(
        session,
        actor.agency_id,
        user_id,
        MembershipChange.model_validate(
            {
                "role": membership.role,
                "all_clients": membership.all_clients,
                "active": False,
            }
        ),
        actor_email=actor.email,
    )
    await session.execute(
        delete(AdminSession).where(
            AdminSession.user_id == user_id,
            AdminSession.agency_id == actor.agency_id,
        )
    )
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
    if agency_rls_enabled():
        rows = (
            await session.execute(
                text("SELECT agency_id, role FROM public.governflow_login_agencies(:identity)"),
                {"identity": user.id},
            )
        ).all()
        memberships = [SimpleNamespace(agency_id=r.agency_id, role=r.role) for r in rows]
    else:
        records = (
            (
                await session.execute(
                    select(AgencyMembership)
                    .join(Agency, Agency.id == AgencyMembership.agency_id)
                    .where(
                        AgencyMembership.user_id == user.id,
                        AgencyMembership.active.is_(True),
                        Agency.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        memberships = [SimpleNamespace(agency_id=r.agency_id, role=r.role) for r in records]
    # A requested agency is a selector only after password and membership verification.
    choices = [m for m in memberships if body.agency_id is None or m.agency_id == body.agency_id]
    if not choices:
        raise HTTPException(401, "No active agency membership")
    if len(choices) > 1:
        raise HTTPException(
            409, {"message": "Choose an agency", "agency_ids": [str(m.agency_id) for m in choices]}
        )
    membership = choices[0]
    await set_agency_context(session, membership.agency_id)
    token = secrets.token_urlsafe(32)
    session.add(
        AdminSession(
            token_hash=token_hash(token),
            user_id=user.id,
            agency_id=membership.agency_id,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {
        "token": token,
        "expires_in": 28800,
        "role": membership.role,
        "agency_id": str(membership.agency_id),
    }


@router.get("/admin/auth/me")
async def me(actor: Actor) -> dict[str, object]:
    return {
        "email": actor.email,
        "role": actor.role,
        "client_id": str(actor.client_id) if actor.client_id else None,
        "agency_id": str(actor.agency_id),
        "client_ids": [str(c) for c in sorted(actor.client_ids)]
        if actor.client_ids is not None
        else None,
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
