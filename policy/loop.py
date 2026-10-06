import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.models import Alert, KillFlag, Workflow
from gateway.errors import GatewayError
from policy.kill import clear_cache


def positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**63 - 1:
        raise GatewayError(503, "invalid_policy", name + " must be a positive integer")
    return cast(int, value)


def loop_policy(policy: dict[str, Any]) -> tuple[int, int, int]:
    budget = policy.get("budget", {})
    loop = policy.get("loop_breaker", {})
    if not isinstance(budget, dict) or not isinstance(loop, dict):
        raise GatewayError(503, "invalid_policy", "Budget or loop policy is invalid")
    cap = min(
        positive(budget.get("per_run_micro", 2**63 - 1), "Per-run cap"),
        positive(loop.get("run_cost_micro", 2**63 - 1), "Loop cost cap"),
    )
    return (
        cap,
        positive(loop.get("max_steps", 100), "Step cap"),
        positive(loop.get("rpm", 120), "Loop RPM"),
    )


async def trip(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    reason: str,
    *,
    run_id: uuid.UUID | None = None,
    cost_micro: int | None = None,
) -> None:
    client_id = (
        await session.execute(select(Workflow.client_id).where(Workflow.id == workflow_id))
    ).scalar_one()
    statement = insert(KillFlag).values(
        scope="workflow", scope_id=str(workflow_id), active=True, reason=reason
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[KillFlag.scope, KillFlag.scope_id],
            set_={"active": True, "reason": reason},
        )
    )
    session.add(
        Alert(
            client_id=client_id,
            workflow_id=workflow_id,
            kind=reason,
            details={"run_id": str(run_id) if run_id else None, "cost_micro": cost_micro},
        )
    )
    await append_event(
        session, AuditEventInput(client_id=client_id, type=reason, cost_micro=cost_micro), run_id
    )
    clear_cache()
