import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Event

ZERO_HASH = bytes(32)


@dataclass(frozen=True)
class AuditEventInput:
    client_id: uuid.UUID
    type: str
    provider: str | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None
    cost_micro: int | None = None
    estimated: bool = False
    content_ref: str | None = None


def canonical_row(event: Event) -> dict[str, Any]:
    return {
        "client_id": str(event.client_id),
        "run_id": str(event.run_id) if event.run_id else None,
        "ts": event.ts.astimezone(UTC).isoformat(),
        "type": event.type,
        "provider": event.provider,
        "model": event.model,
        "usage": event.usage,
        "cost_micro": event.cost_micro,
        "estimated": event.estimated,
        "content_ref": event.content_ref,
    }


def event_hash(previous: bytes, row: dict[str, Any]) -> bytes:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(previous + payload).digest()


async def append_event(
    session: AsyncSession, event_input: AuditEventInput, run_id: uuid.UUID | None = None
) -> Event:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:client_id))"),
        {"client_id": str(event_input.client_id)},
    )
    previous = (
        await session.execute(
            select(Event.hash)
            .where(Event.client_id == event_input.client_id)
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or ZERO_HASH
    event = Event(
        client_id=event_input.client_id,
        run_id=run_id,
        ts=datetime.now(UTC),
        type=event_input.type,
        provider=event_input.provider,
        model=event_input.model,
        usage=event_input.usage,
        cost_micro=event_input.cost_micro,
        estimated=event_input.estimated,
        content_ref=event_input.content_ref,
        prev_hash=previous,
        hash=b"",
    )
    event.hash = event_hash(previous, canonical_row(event))
    session.add(event)
    await session.flush()
    return event


async def verify_chain(session: AsyncSession, client_id: uuid.UUID) -> tuple[bool, int | None]:
    previous = ZERO_HASH
    result = await session.stream(
        select(Event).where(Event.client_id == client_id).order_by(Event.id)
    )
    async for row in result.scalars():
        if row.prev_hash != previous or row.hash != event_hash(previous, canonical_row(row)):
            return False, row.id
        previous = row.hash
    return True, None
