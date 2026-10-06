import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from admin_api.auth import Actor, DbSession, client_scope, permit, workflow_scope
from approvals.service import content
from core.models import (
    Alert,
    Approval,
    Budget,
    Client,
    Event,
    KillFlag,
    ProviderAccount,
    ReconcileJob,
    ReconcileSnapshot,
    VirtualKey,
    Workflow,
)

router = APIRouter()


@router.get("/admin/clients")
async def clients(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    query = select(Client).order_by(Client.name)
    if actor.client_id:
        query = query.where(Client.id == actor.client_id)
    rows = (await session.execute(query)).scalars().all()
    return [{"id": str(r.id), "name": r.name, "status": r.status} for r in rows]


@router.get("/admin/workflows")
async def workflows(
    session: DbSession, actor: Actor, client_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    query = select(Workflow).order_by(Workflow.name)
    scope = client_id or actor.client_id
    if scope:
        client_scope(actor, scope)
        query = query.where(Workflow.client_id == scope)
    rows = (await session.execute(query)).scalars().all()
    return [
        {
            "id": str(r.id),
            "client_id": str(r.client_id),
            "name": r.name,
            "risk_class": r.risk_class,
            "policy": r.policy,
            "status": r.status,
        }
        for r in rows
    ]


@router.get("/admin/spend")
async def spend(
    session: DbSession,
    actor: Actor,
    period: str = Query(default="", pattern=r"^(|\d{4}-(0[1-9]|1[0-2]))$"),
    client_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    period = period or datetime.now(UTC).strftime("%Y-%m")
    scope = client_id or actor.client_id
    if scope:
        client_scope(actor, scope)
    query = (
        select(Workflow, Client, Budget)
        .join(Client, Client.id == Workflow.client_id)
        .outerjoin(Budget, (Budget.workflow_id == Workflow.id) & (Budget.period == period))
        .order_by(Client.name, Workflow.name)
    )
    if scope:
        query = query.where(Workflow.client_id == scope)
    rows = (await session.execute(query)).all()
    groups: dict[str, Any] = {}
    items = []
    for workflow, client, budget in rows:
        item = {
            "id": str(workflow.id),
            "client_id": str(client.id),
            "client": client.name,
            "name": workflow.name,
            "risk_class": workflow.risk_class,
            "spent_micro": budget.spent_micro if budget else 0,
            "reserved_micro": budget.reserved_micro if budget else 0,
            "cap_micro": budget.cap_micro if budget else None,
        }
        items.append(item)
        group = groups.setdefault(
            str(client.id),
            {"id": str(client.id), "name": client.name, "spent_micro": 0, "reserved_micro": 0},
        )
        group["spent_micro"] += item["spent_micro"]
        group["reserved_micro"] += item["reserved_micro"]
    try:
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
        if start.year >= 9999:
            raise ValueError("year")
    except ValueError as exc:
        raise HTTPException(400, "Invalid reporting month") from exc
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    daily_date = func.date(func.timezone("UTC", Event.ts))
    daily_query = select(
        daily_date,
        func.coalesce(func.sum(Event.cost_micro), 0),
        func.count(),
    ).where(Event.type == "provider_call", Event.ts >= start, Event.ts < end)
    if scope:
        daily_query = daily_query.where(Event.client_id == scope)
    daily = (await session.execute(daily_query.group_by(daily_date).order_by(daily_date))).all()
    return {
        "period": period,
        "clients": list(groups.values()),
        "workflows": items,
        "daily": [
            {"day": str(day), "cost_micro": cost, "calls": count} for day, cost, count in daily
        ],
    }


@router.get("/admin/keys")
async def keys(
    session: DbSession, actor: Actor, workflow_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    query = select(VirtualKey).join(Workflow, Workflow.id == VirtualKey.workflow_id)
    if actor.client_id:
        query = query.where(Workflow.client_id == actor.client_id)
    if workflow_id:
        await workflow_scope(session, actor, workflow_id)
        query = query.where(VirtualKey.workflow_id == workflow_id)
    rows = (await session.execute(query.order_by(VirtualKey.created_at.desc()))).scalars().all()
    return [
        {
            "id": str(r.id),
            "workflow_id": str(r.workflow_id),
            "prefix": r.prefix,
            "created_at": r.created_at.isoformat(),
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
        }
        for r in rows
    ]


@router.get("/admin/approvals")
async def approvals(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    query = select(Approval).join(Workflow, Workflow.id == Approval.workflow_id)
    if actor.client_id:
        query = query.where(Workflow.client_id == actor.client_id)
    rows = (
        (await session.execute(query.order_by(Approval.created_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "workflow_id": str(r.workflow_id),
            "action_name": r.action_name,
            "summary": r.action_summary,
            "status": r.status,
            "expires_at": r.expires_at.isoformat(),
            "approver": r.approver,
            "consumed": r.consumed_at is not None,
        }
        for r in rows
    ]


@router.get("/admin/actions/{approval_id}")
async def action(approval_id: uuid.UUID, session: DbSession, actor: Actor) -> dict[str, Any]:
    permit(actor, "admin", "approver")
    record = await session.get(Approval, approval_id)
    if record is None:
        raise HTTPException(404, "Approval not found")
    workflow = await workflow_scope(session, actor, record.workflow_id)
    return {
        "id": str(record.id),
        "action_name": record.action_name,
        "summary": record.action_summary,
        "status": record.status,
        "action": content(record, workflow.client_id),
    }


@router.get("/admin/alerts")
async def alerts(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    query = select(Alert)
    if actor.client_id:
        query = query.where(Alert.client_id == actor.client_id)
    rows = (
        (await session.execute(query.order_by(Alert.created_at.desc()).limit(100))).scalars().all()
    )
    return [
        {
            "id": str(r.id),
            "client_id": str(r.client_id),
            "workflow_id": str(r.workflow_id) if r.workflow_id else None,
            "kind": r.kind,
            "details": r.details,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/admin/kill")
async def kills(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    rows = (await session.execute(select(KillFlag))).scalars().all()
    workflow_ids = (
        set(
            (
                await session.execute(
                    select(Workflow.id).where(Workflow.client_id == actor.client_id)
                )
            ).scalars()
        )
        if actor.client_id
        else set()
    )
    return [
        {"scope": r.scope, "scope_id": r.scope_id, "active": r.active, "reason": r.reason}
        for r in rows
        if actor.client_id is None
        or r.scope == "global"
        or (r.scope == "client" and r.scope_id == str(actor.client_id))
        or (r.scope == "workflow" and r.scope_id in {str(w) for w in workflow_ids})
    ]


@router.get("/admin/reconciliation")
async def reconciliation(session: DbSession, actor: Actor) -> dict[str, Any]:
    job_query = select(ReconcileJob).order_by(ReconcileJob.day.desc()).limit(90)
    snapshot_query = select(ReconcileSnapshot).order_by(ReconcileSnapshot.day.desc()).limit(1000)
    account_query = select(ProviderAccount)
    if actor.client_id:
        job_query = job_query.where(ReconcileJob.client_id == actor.client_id)
        snapshot_query = snapshot_query.where(ReconcileSnapshot.client_id == actor.client_id)
        account_query = account_query.where(ProviderAccount.client_id == actor.client_id)
    jobs = (await session.execute(job_query)).scalars().all()
    snapshots = (await session.execute(snapshot_query)).scalars().all()
    accounts = (await session.execute(account_query)).scalars().all()
    return {
        "jobs": [
            {
                "client_id": str(r.client_id),
                "provider": r.provider,
                "day": str(r.day),
                "status": r.status,
                "error": r.error,
                "fetched_at": r.fetched_at.isoformat(),
            }
            for r in jobs
        ],
        "snapshots": [
            {
                "client_id": str(r.client_id),
                "provider": r.provider,
                "day": str(r.day),
                "kind": r.kind,
                "dimension": r.dimension,
                "gateway": r.gateway,
                "observed": r.observed,
            }
            for r in snapshots
        ],
        "accounts": [
            {
                "client_id": str(r.client_id),
                "provider": r.provider,
                "external_id": r.external_id,
                "validated": r.validated_at is not None,
            }
            for r in accounts
        ],
    }
