"""Release reservations left by interrupted gateway processes."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.database import session_factory
from core.models import Budget, Reservation, Run, Workflow


async def reap_stale(session: AsyncSession, before: datetime, limit: int = 100) -> int:
    rows = (
        await session.execute(
            select(Reservation, Workflow.client_id)
            .join(Budget, Reservation.budget_id == Budget.id)
            .join(Workflow, Budget.workflow_id == Workflow.id)
            .where(Reservation.status == "reserved", Reservation.created_at < before)
            .order_by(Reservation.created_at, Reservation.id)
            .limit(limit)
            .with_for_update(of=Reservation, skip_locked=True)
        )
    ).all()
    for record, client_id in rows:
        await session.execute(
            update(Budget)
            .where(Budget.id == record.budget_id)
            .values(reserved_micro=Budget.reserved_micro - record.est_micro)
        )
        await session.execute(
            update(Run)
            .where(Run.id == record.run_id)
            .values(reserved_micro=Run.reserved_micro - record.est_micro)
        )
        record.status = "released"
        record.settled_at = datetime.now(UTC)
        await append_event(
            session,
            AuditEventInput(client_id=client_id, type="reservation_reaped", cost_micro=0),
            record.run_id,
        )
    await session.commit()
    return len(rows)


async def main() -> None:
    total = 0
    while True:
        async with session_factory() as session:
            count = await reap_stale(session, datetime.now(UTC) - timedelta(hours=1))
        total += count
        if count < 100:
            break
    print(f"Released {total} stale reservations")


if __name__ == "__main__":
    asyncio.run(main())
