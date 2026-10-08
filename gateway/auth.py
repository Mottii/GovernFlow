import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.models import DEFAULT_AGENCY_ID, Agency, Client, VirtualKey, Workflow
from core.security import virtual_key_hash
from gateway.errors import GatewayError


@dataclass(frozen=True)
class AuthContext:
    client_id: uuid.UUID
    workflow_id: uuid.UUID
    risk_class: str
    policy: dict[str, Any]
    key_prefix: str
    agency_id: uuid.UUID = DEFAULT_AGENCY_ID


async def authenticate_openai(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise GatewayError(401, "invalid_api_key", "A valid virtual key is required")
    return await resolve_virtual_key(session, authorization[7:])


async def authenticate_anthropic(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> AuthContext:
    if not x_api_key:
        raise GatewayError(401, "invalid_api_key", "A valid virtual key is required")
    return await resolve_virtual_key(session, x_api_key)


async def resolve_virtual_key(session: AsyncSession, key: str) -> AuthContext:
    if not key.startswith("gk_"):
        raise GatewayError(401, "invalid_api_key", "A valid virtual key is required")
    row = (
        await session.execute(
            select(VirtualKey, Workflow, Client)
            .join(Workflow, VirtualKey.workflow_id == Workflow.id)
            .join(Client, Workflow.client_id == Client.id)
            .join(Agency, Agency.id == Client.agency_id)
            .where(
                VirtualKey.key_hash == virtual_key_hash(key),
                VirtualKey.revoked_at.is_(None),
                Workflow.status == "active",
                Client.status == "active",
                Agency.status == "active",
            )
        )
    ).one_or_none()
    if row is None:
        raise GatewayError(401, "invalid_api_key", "A valid virtual key is required")
    record, workflow, client = row
    return AuthContext(
        client_id=client.id,
        workflow_id=workflow.id,
        risk_class=workflow.risk_class,
        policy=workflow.policy,
        key_prefix=record.prefix,
        agency_id=client.agency_id,
    )
