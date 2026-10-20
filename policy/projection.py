"""Current call charges plus unchanged legacy audit history; corrections count once."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Event, ProviderCall, Run


@dataclass(frozen=True)
class CallFact:
    client_id: uuid.UUID
    workflow_id: uuid.UUID | None
    provider: str | None
    model: str | None
    at: datetime
    cost_micro: int
    calls: int
    estimated_calls: int
    corrected_calls: int
    usage: dict[str, Any] | None
    legacy: bool = False


async def facts(
    session: AsyncSession,
    client_ids: list[uuid.UUID],
    start: datetime,
    end: datetime,
    *,
    period: str | None = None,
    provider: str | None = None,
) -> list[CallFact]:
    query = select(ProviderCall).where(
        ProviderCall.client_id.in_(client_ids),
        ProviderCall.state.in_(("settled_estimated", "settled_measured")),
    )
    if period:
        query = query.where(ProviderCall.period == period)
    else:
        query = query.where(ProviderCall.created_at >= start, ProviderCall.created_at < end)
    if provider:
        query = query.where(ProviderCall.provider == provider)
    rows = (await session.execute(query)).scalars()
    result = []
    for call in rows:
        adopted = call.legacy_baseline_micro is not None
        result.append(
            CallFact(
                call.client_id,
                call.workflow_id,
                call.provider,
                call.model,
                call.created_at,
                call.charged_micro - (call.legacy_baseline_micro or 0),
                0 if adopted else 1,
                int(call.state == "settled_estimated" and not adopted),
                int(call.revision > 1 or adopted),
                None if adopted else call.usage,
                adopted,
            )
        )
    legacy = (
        select(Event, Run.workflow_id)
        .outerjoin(Run, Run.id == Event.run_id)
        .where(
            Event.client_id.in_(client_ids),
            Event.type == "provider_call",
            Event.ts >= start,
            Event.ts < end,
            or_(Event.content_ref.is_(None), ~Event.content_ref.startswith("provider-call:")),
        )
    )
    if provider:
        legacy = legacy.where(Event.provider == provider)
    for event, workflow_id in (await session.execute(legacy)).all():
        result.append(
            CallFact(
                event.client_id,
                workflow_id,
                event.provider,
                event.model,
                event.ts,
                event.cost_micro or 0,
                1,
                int(event.estimated),
                0,
                event.usage,
                True,
            )
        )
    return result
