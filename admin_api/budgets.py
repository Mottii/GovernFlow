"""Scoped, version-checked budgets, monthly templates, and run stops."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from admin_api.auth import Actor, DbSession, client_scope, permit, permitted_clients, workflow_scope
from audit.administration import record
from core.models import Budget, BudgetTemplate, ClientBudget, Run, Workflow
from policy.accounting import database_now
from policy.allocations import client_bucket, period_lock, workflow_bucket
from policy.loop import loop_policy

router = APIRouter()
PERIOD = r"^\d{4}-(0[1-9]|1[0-2])$"


class CapChange(BaseModel):
    cap_micro: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=200)


class ClientCap(CapChange):
    period: str = Field(pattern=PERIOD)


class TemplateChange(BaseModel):
    scope: Literal["client", "workflow"]
    scope_id: uuid.UUID
    cap_micro: int = Field(ge=0, le=2**63 - 1, strict=True)
    enabled: bool = True
    expected_revision: int = Field(ge=0)
    effective_period: str | None = Field(default=None, pattern=PERIOD)
    reason: str = Field(min_length=1, max_length=200)


class StopChange(BaseModel):
    expected_revision: int = Field(ge=0)
    active: bool
    reason: str = Field(min_length=1, max_length=200)


def budget_view(bucket: Any) -> dict[str, Any]:
    return {
        "id": str(bucket.id),
        "period": bucket.period,
        "cap_micro": bucket.cap_micro,
        "spent_micro": bucket.spent_micro,
        "reserved_micro": bucket.reserved_micro,
        "headroom_micro": None
        if bucket.cap_micro is None
        else bucket.cap_micro - bucket.spent_micro - bucket.reserved_micro,
        "revision": bucket.revision,
        "template_revision_id": str(bucket.template_revision_id)
        if bucket.template_revision_id
        else None,
    }


async def shared_for_client(
    session: DbSession, client_id: uuid.UUID, agency_id: uuid.UUID, period: str
) -> ClientBudget:
    await period_lock(session, client_id, period)
    workflow = (
        await session.execute(select(Workflow).where(Workflow.client_id == client_id).limit(1))
    ).scalar_one_or_none()
    if workflow:
        return await client_bucket(session, workflow, period)
    bucket = (
        await session.execute(
            select(ClientBudget)
            .where(ClientBudget.client_id == client_id, ClientBudget.period == period)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if bucket is None:
        bucket = ClientBudget(agency_id=agency_id, client_id=client_id, period=period)
        session.add(bucket)
        await session.flush()
    return bucket


@router.get("/admin/client-budgets")
async def client_budgets(
    session: DbSession, actor: Actor, period: str | None = Query(None, pattern=PERIOD)
) -> list[dict[str, Any]]:
    query = select(ClientBudget).where(ClientBudget.client_id.in_(permitted_clients(actor)))
    if period:
        query = query.where(ClientBudget.period == period)
    return [
        {**budget_view(b), "client_id": str(b.client_id)}
        for b in (
            await session.execute(query.order_by(ClientBudget.period.desc(), ClientBudget.id))
        ).scalars()
    ]


@router.put("/admin/clients/{client_id}/budget")
async def client_cap(
    client_id: uuid.UUID, body: ClientCap, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    await client_scope(session, actor, client_id)
    bucket = await shared_for_client(session, client_id, actor.agency_id, body.period)
    if bucket.revision != body.expected_revision:
        raise HTTPException(409, {"revision": bucket.revision})
    old = bucket.cap_micro
    bucket.cap_micro, bucket.revision = body.cap_micro, bucket.revision + 1
    await record(
        session,
        actor.agency_id,
        actor.email,
        "client_budget",
        bucket.id,
        "cap_changed",
        period=body.period,
        previous_cap_micro=old,
        cap_micro=body.cap_micro,
        reason=body.reason,
    )
    await session.commit()
    return budget_view(bucket)


@router.patch("/admin/budgets/{budget_id}")
async def workflow_cap(
    budget_id: uuid.UUID, body: CapChange, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    bucket = await session.get(Budget, budget_id)
    if bucket is None:
        raise HTTPException(404, "Budget not found")
    workflow = await workflow_scope(session, actor, bucket.workflow_id)
    await client_bucket(session, workflow, bucket.period)
    bucket = await workflow_bucket(session, workflow, bucket.period)
    assert bucket is not None
    if body.cap_micro is None:
        raise HTTPException(400, "A workflow cap is required")
    if bucket.revision != body.expected_revision:
        raise HTTPException(409, {"revision": bucket.revision})
    old = bucket.cap_micro
    bucket.cap_micro, bucket.revision = body.cap_micro, bucket.revision + 1
    await record(
        session,
        actor.agency_id,
        actor.email,
        "workflow_budget",
        bucket.id,
        "cap_changed",
        previous_cap_micro=old,
        cap_micro=body.cap_micro,
        period=bucket.period,
        reason=body.reason,
    )
    await session.commit()
    return budget_view(bucket)


@router.get("/admin/budget-templates")
async def templates(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(BudgetTemplate)
            .where(BudgetTemplate.client_id.in_(permitted_clients(actor)))
            .order_by(BudgetTemplate.created_at.desc())
        )
    ).scalars()
    return [
        {
            "id": str(b.id),
            "scope": b.scope,
            "scope_id": str(b.scope_id),
            "revision": b.revision,
            "cap_micro": b.cap_micro,
            "enabled": b.enabled,
            "effective_period": b.effective_period,
        }
        for b in rows
    ]


@router.post("/admin/budget-templates", status_code=201)
async def save_template(body: TemplateChange, session: DbSession, actor: Actor) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    if body.scope == "workflow":
        workflow = await workflow_scope(session, actor, body.scope_id)
        client_id = workflow.client_id
    else:
        await client_scope(session, actor, body.scope_id)
        client_id = body.scope_id
    # Templates serialize on a stable guard in addition to the period guards used by money.
    await period_lock(session, client_id, "templates")
    revision = (
        await session.execute(
            select(func.coalesce(func.max(BudgetTemplate.revision), 0)).where(
                BudgetTemplate.scope == body.scope, BudgetTemplate.scope_id == body.scope_id
            )
        )
    ).scalar_one()
    if revision != body.expected_revision:
        raise HTTPException(409, {"revision": revision})
    now = await database_now(session)
    following = (
        now.replace(year=now.year + 1, month=1)
        if now.month == 12
        else now.replace(month=now.month + 1)
    )
    effective = body.effective_period or following.strftime("%Y-%m")
    if effective < now.strftime("%Y-%m"):
        raise HTTPException(400, "Template cannot change historical months")
    item = BudgetTemplate(
        agency_id=actor.agency_id,
        client_id=client_id,
        workflow_id=body.scope_id if body.scope == "workflow" else None,
        scope=body.scope,
        scope_id=body.scope_id,
        revision=revision + 1,
        cap_micro=body.cap_micro,
        enabled=body.enabled,
        effective_period=effective,
        actor=actor.email,
    )
    session.add(item)
    await record(
        session,
        actor.agency_id,
        actor.email,
        "budget_template",
        body.scope_id,
        "revision_created",
        scope=body.scope,
        version=revision + 1,
        effective_period=effective,
        cap_micro=body.cap_micro,
        enabled=body.enabled,
        reason=body.reason,
    )
    await session.commit()
    return {"id": str(item.id), "revision": item.revision, "effective_period": effective}


@router.get("/admin/run-stops")
async def run_stops(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Run)
            .join(Workflow, Workflow.id == Run.workflow_id)
            .where(Workflow.client_id.in_(permitted_clients(actor)), Run.stopped.is_(True))
            .order_by(Run.started_at.desc())
            .limit(200)
        )
    ).scalars()
    return [
        {
            "id": str(r.id),
            "workflow_id": str(r.workflow_id),
            "external_run_id": r.external_run_id,
            "reason": r.stop_reason,
            "revision": r.stop_revision,
            "cost_micro": r.cost_micro,
            "step_count": r.step_count,
        }
        for r in rows
    ]


@router.put("/admin/runs/{run_id}/stop")
async def stop(
    run_id: uuid.UUID, body: StopChange, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    workflow = await workflow_scope(session, actor, run.workflow_id)
    period = (await database_now(session)).strftime("%Y-%m")
    shared = await client_bucket(session, workflow, period)
    budget = await workflow_bucket(session, workflow, period)
    run = (
        await session.execute(
            select(Run)
            .where(Run.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if run.stop_revision != body.expected_revision:
        raise HTTPException(409, {"revision": run.stop_revision})
    cap, steps, _ = loop_policy(workflow.policy)
    if not body.active and (
        run.cost_micro + run.reserved_micro >= cap
        or run.step_count >= steps
        or budget is None
        or budget.spent_micro + budget.reserved_micro >= budget.cap_micro
        or (
            shared.cap_micro is not None
            and shared.spent_micro + shared.reserved_micro >= shared.cap_micro
        )
    ):
        raise HTTPException(409, "Raise exhausted limits before clearing the stop")
    run.stopped, run.stop_reason, run.cap_micro = body.active, body.reason, cap
    run.stop_revision += 1
    await record(
        session,
        actor.agency_id,
        actor.email,
        "run",
        run_id,
        "stop_changed",
        active=body.active,
        reason=body.reason,
    )
    await session.commit()
    return {"active": run.stopped, "revision": run.stop_revision}
