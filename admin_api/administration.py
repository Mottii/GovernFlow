"""Agency-scoped administrative history."""

from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from admin_api.auth import Actor, DbSession, permit
from core.models import AdministrativeEvent

router = APIRouter()


@router.get("/admin/administrative-events")
async def events(
    session: DbSession,
    actor: Actor,
    response: Response,
    cursor: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    permit(actor, "admin")
    rows = list(
        (
            await session.execute(
                select(AdministrativeEvent)
                .where(
                    AdministrativeEvent.agency_id == actor.agency_id,
                    AdministrativeEvent.id > cursor,
                )
                .order_by(AdministrativeEvent.id)
                .limit(limit + 1)
            )
        ).scalars()
    )
    if len(rows) > limit:
        response.headers["X-Next-Cursor"] = str(rows[limit - 1].id)
    return [
        {
            "id": e.id,
            "actor": e.actor,
            "resource": e.resource,
            "resource_id": e.resource_id,
            "action": e.action,
            "details": e.details,
            "created_at": e.created_at.isoformat(),
            "hash": e.hash.hex(),
            "previous_hash": e.previous_hash.hex(),
        }
        for e in rows[:limit]
    ]
