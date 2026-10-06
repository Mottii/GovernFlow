import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.consume import ActionConsume, consume
from approvals.expiry import expire_pending
from approvals.service import ActionCheck, check, scoped, view
from core.database import get_session
from gateway.auth import AuthContext, authenticate_openai
from policy.kill import check_kill

router = APIRouter()
Auth = Annotated[AuthContext, Depends(authenticate_openai)]
Db = Annotated[AsyncSession, Depends(get_session)]


@router.post("/v1/actions/check")
async def check_action(body: ActionCheck, auth: Auth, session: Db) -> dict[str, Any]:
    return await check(session, auth, body)


@router.get("/v1/actions/{approval_id}")
async def poll_action(approval_id: uuid.UUID, auth: Auth, session: Db) -> dict[str, Any]:
    await check_kill(session, auth)
    record = await scoped(session, auth, approval_id)
    await expire_pending(session, approval_id)
    return view(record)


@router.post("/v1/actions/consume")
async def consume_action(body: ActionConsume, auth: Auth, session: Db) -> dict[str, Any]:
    return await consume(session, auth, body)
