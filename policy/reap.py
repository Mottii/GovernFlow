"""Void undispatched reservations and conservatively charge expired dispatches."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import agency_sessions
from core.models import Reservation


async def reap_stale(session: AsyncSession, before: datetime, limit: int = 100) -> int:
    from core.models import ProviderCall
    from policy.accounting import Evidence, apply, database_now, locked_call

    # Metadata-created and historical reservations may not have a call yet.
    legacy = (
        (
            await session.execute(
                select(Reservation.id)
                .outerjoin(ProviderCall, ProviderCall.reservation_id == Reservation.id)
                .where(
                    Reservation.status == "reserved",
                    Reservation.created_at < before,
                    ProviderCall.id.is_(None),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for identity in legacy:
        adopted = await locked_call(session, identity)
        adopted.lease_until = await database_now(session)
    await session.commit()
    identities = list(
        (
            await session.execute(
                select(ProviderCall.id)
                .where(
                    ProviderCall.state.in_(("reserved", "dispatched")),
                    ProviderCall.lease_until <= func.clock_timestamp(),
                )
                .order_by(ProviderCall.lease_until, ProviderCall.id)
                .limit(limit)
            )
        ).scalars()
    )
    await session.commit()
    done = 0
    for identity in identities:
        call = (
            await session.execute(
                select(ProviderCall)
                .where(
                    ProviderCall.id == identity,
                    ProviderCall.state.in_(("reserved", "dispatched")),
                    ProviderCall.lease_until <= func.clock_timestamp(),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if call is None:
            await session.rollback()
            continue
        kind: Literal["void", "estimate"] = "void" if call.state == "reserved" else "estimate"
        await apply(
            session,
            Evidence(
                evidence_id=uuid.uuid5(call.id, "expired:" + kind),
                agency_id=call.agency_id,
                client_id=call.client_id,
                call_id=call.id,
                kind=kind,
                amount_micro=0 if kind == "void" else call.estimate_micro,
                reason="undispatched_lease_expired" if kind == "void" else "dispatch_lease_expired",
            ),
        )
        await session.commit()
        done += 1
    return done


async def main() -> None:
    total = 0
    async for session in agency_sessions():
        while True:
            count = await reap_stale(session, datetime.now(UTC) - timedelta(hours=1))
            total += count
            if count < 100:
                break
    print(f"Recovered {total} expired provider calls")


if __name__ == "__main__":
    asyncio.run(main())
