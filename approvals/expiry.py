import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.models import Approval, Workflow


async def expire_record(session: AsyncSession, record: Approval) -> bool:
    if record.status != "pending" or record.expires_at > datetime.now(UTC):
        return False
    record.status = "expired"
    record.decided_at = datetime.now(UTC)
    record.approver = "timeout_policy"
    client_id = (
        await session.execute(select(Workflow.client_id).where(Workflow.id == record.workflow_id))
    ).scalar_one()
    await append_event(
        session,
        AuditEventInput(
            client_id,
            "action_expired",
            content_ref="approval:" + str(record.id),
            usage={"outcome": record.timeout_outcome},
        ),
    )
    return True


async def expire_pending(session: AsyncSession, approval_id: uuid.UUID | None = None) -> int:
    statement = select(Approval).where(
        Approval.status == "pending", Approval.expires_at <= datetime.now(UTC)
    )
    if approval_id is not None:
        statement = statement.where(Approval.id == approval_id)
    records = (
        (await session.execute(statement.with_for_update(skip_locked=True).limit(500)))
        .scalars()
        .all()
    )
    for record in records:
        await expire_record(session, record)
    await session.commit()
    return len(records)


async def main() -> None:
    from core.database import session_factory

    async with session_factory() as session:
        print(f"Expired {await expire_pending(session)} approvals")


if __name__ == "__main__":
    asyncio.run(main())
