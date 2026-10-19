"""Read-only renewal and stop metadata shared by dashboard and reports."""

import uuid
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import BudgetTemplate, Run, Workflow


async def governance_snapshot(session: AsyncSession, client_ids: list[uuid.UUID]) -> dict[str, Any]:
    latest = (
        select(
            BudgetTemplate.scope,
            BudgetTemplate.scope_id,
            func.max(BudgetTemplate.revision).label("revision"),
        )
        .where(BudgetTemplate.client_id.in_(client_ids))
        .group_by(BudgetTemplate.scope, BudgetTemplate.scope_id)
        .subquery()
    )
    templates = (
        await session.execute(
            select(BudgetTemplate)
            .join(
                latest,
                and_(
                    BudgetTemplate.scope == latest.c.scope,
                    BudgetTemplate.scope_id == latest.c.scope_id,
                    BudgetTemplate.revision == latest.c.revision,
                ),
            )
            .where(BudgetTemplate.client_id.in_(client_ids))
            .order_by(BudgetTemplate.scope, BudgetTemplate.scope_id)
        )
    ).scalars()
    stops = (
        await session.execute(
            select(Run)
            .join(Workflow, Workflow.id == Run.workflow_id)
            .where(Workflow.client_id.in_(client_ids), Run.stopped.is_(True))
            .order_by(Run.started_at.desc())
            .limit(200)
        )
    ).scalars()
    return {
        "renewal_templates": [
            {
                "id": str(t.id),
                "scope": t.scope,
                "scope_id": str(t.scope_id),
                "revision": t.revision,
                "enabled": t.enabled,
                "cap_micro": t.cap_micro,
                "effective_period": t.effective_period,
            }
            for t in templates
        ],
        "run_stops": [
            {
                "id": str(r.id),
                "workflow_id": str(r.workflow_id),
                "external_run_id": r.external_run_id,
                "reason": r.stop_reason,
                "revision": r.stop_revision,
            }
            for r in stops
        ],
    }
