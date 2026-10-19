import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.models import Approval, Workflow


async def expire_record(session: AsyncSession, record: Approval) -> bool:
    if (
        record.invalidated_at is not None
        or record.status != "pending"
        or record.expires_at > datetime.now(UTC)
    ):
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
            usage={
                "outcome": record.timeout_outcome,
                "policy_version": record.policy_version,
                "matched_rules": record.matched_rules,
            },
        ),
    )
    return True


async def expire_pending(session: AsyncSession, approval_id: uuid.UUID | None = None) -> int:
    from policy.revisions import lock

    statement = select(Approval.id, Approval.workflow_id).where(
        Approval.status == "pending", Approval.expires_at <= datetime.now(UTC)
    )
    if approval_id is not None:
        statement = statement.where(Approval.id == approval_id)
    candidates = (await session.execute(statement.limit(500))).all()
    await session.commit()
    count = 0
    for identity, workflow_id in candidates:
        workflow = await lock(session, workflow_id)
        record = (
            await session.execute(
                select(Approval)
                .where(Approval.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if record is not None and record.policy_version == workflow.policy_version:
            count += int(await expire_record(session, record))
        await session.commit()
    return count


async def main() -> None:
    from core.database import agency_sessions

    async for session in agency_sessions():
        print(f"Expired {await expire_pending(session)} approvals")


if __name__ == "__main__":
    asyncio.run(main())
