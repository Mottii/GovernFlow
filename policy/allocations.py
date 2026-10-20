"""Shared monthly accounting and idempotent UTC renewal."""

import asyncio
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Budget, BudgetTemplate, ClientBudget, Workflow


async def period_lock(session: AsyncSession, client_id: uuid.UUID, period: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"client-period:{client_id}:{period}"},
    )


async def template(
    session: AsyncSession, scope: str, identity: uuid.UUID, period: str
) -> BudgetTemplate | None:
    return (
        await session.execute(
            select(BudgetTemplate)
            .where(
                BudgetTemplate.scope == scope,
                BudgetTemplate.scope_id == identity,
                BudgetTemplate.effective_period <= period,
            )
            .order_by(BudgetTemplate.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def client_bucket(session: AsyncSession, workflow: Workflow, period: str) -> ClientBudget:
    await period_lock(session, workflow.client_id, period)
    bucket = (
        await session.execute(
            select(ClientBudget)
            .where(
                ClientBudget.client_id == workflow.client_id,
                ClientBudget.period == period,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if bucket is None:
        totals = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Budget.spent_micro), 0),
                    func.coalesce(func.sum(Budget.reserved_micro), 0),
                )
                .join(Workflow, Workflow.id == Budget.workflow_id)
                .where(Workflow.client_id == workflow.client_id, Budget.period == period)
            )
        ).one()
        recurring = await template(session, "client", workflow.client_id, period)
        bucket = ClientBudget(
            agency_id=workflow.agency_id,
            client_id=workflow.client_id,
            period=period,
            spent_micro=int(totals[0]),
            reserved_micro=int(totals[1]),
            cap_micro=recurring.cap_micro if recurring and recurring.enabled else None,
            template_revision_id=recurring.id if recurring and recurring.enabled else None,
        )
        session.add(bucket)
        await session.flush()
    return bucket


async def workflow_bucket(session: AsyncSession, workflow: Workflow, period: str) -> Budget | None:
    # Caller has already acquired the shared client-period guard.
    bucket = (
        await session.execute(
            select(Budget)
            .where(
                Budget.workflow_id == workflow.id,
                Budget.period == period,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if bucket is None:
        recurring = await template(session, "workflow", workflow.id, period)
        if recurring and recurring.enabled:
            bucket = Budget(
                agency_id=workflow.agency_id,
                workflow_id=workflow.id,
                period=period,
                cap_micro=recurring.cap_micro,
                template_revision_id=recurring.id,
            )
            session.add(bucket)
            await session.flush()
    return bucket


async def renew_current(session: AsyncSession) -> int:
    from policy.accounting import database_now

    period = (await database_now(session)).strftime("%Y-%m")
    workflows = list((await session.execute(select(Workflow))).scalars())
    count = 0
    for workflow in workflows:
        await client_bucket(session, workflow, period)
        if await workflow_bucket(session, workflow, period):
            count += 1
        await session.commit()
    return count


async def main() -> None:
    from core.database import agency_sessions

    async for session in agency_sessions():
        await renew_current(session)


if __name__ == "__main__":
    asyncio.run(main())
