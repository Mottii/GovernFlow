import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from admin_api.auth import Actor, DbSession, client_scope, permit, permitted_clients, workflow_scope
from admin_api.governance_views import governance_snapshot
from approvals.service import content
from core.models import (
    Alert,
    Approval,
    Budget,
    Client,
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
    query = select(Client).where(Client.id.in_(permitted_clients(actor))).order_by(Client.name)
    rows = (await session.execute(query)).scalars().all()
    return [{"id": str(r.id), "name": r.name, "status": r.status} for r in rows]


@router.get("/admin/workflows")
async def workflows(
    session: DbSession, actor: Actor, client_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    query = (
        select(Workflow)
        .where(Workflow.client_id.in_(permitted_clients(actor)))
        .order_by(Workflow.name)
    )
    scope = client_id
    if scope:
        await client_scope(session, actor, scope)
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
    scope = client_id
    if scope:
        await client_scope(session, actor, scope)
    query = (
        select(Workflow, Client, Budget)
        .join(Client, Client.id == Workflow.client_id)
        .outerjoin(Budget, (Budget.workflow_id == Workflow.id) & (Budget.period == period))
        .where(Client.id.in_(permitted_clients(actor)))
        .order_by(Client.name, Workflow.name)
    )
    if scope:
        query = query.where(Workflow.client_id == scope)
    rows = (await session.execute(query)).all()
    groups: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    for workflow, client, budget in rows:
        item = {
            "budget_id": str(budget.id) if budget else None,
            "budget_revision": budget.revision if budget else None,
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
    from core.models import ClientBudget

    shared = (
        await session.execute(
            select(ClientBudget).where(
                ClientBudget.client_id.in_([uuid.UUID(identity) for identity in groups]),
                ClientBudget.period == period,
            )
        )
    ).scalars()
    for bucket in shared:
        group = groups.get(str(bucket.client_id))
        if group is not None:
            group.update(
                {
                    "cap_micro": bucket.cap_micro,
                    "revision": bucket.revision,
                    "headroom_micro": None
                    if bucket.cap_micro is None
                    else bucket.cap_micro - bucket.spent_micro - bucket.reserved_micro,
                    "template_revision_id": str(bucket.template_revision_id)
                    if bucket.template_revision_id
                    else None,
                }
            )
    from policy.projection import facts

    records = await facts(
        session, [uuid.UUID(identity) for identity in groups], start, end, period=period
    )
    daily_groups: dict[str, dict[str, int]] = {}
    by_workflow = {item["id"]: item for item in items}
    for item in items:
        item["estimated_micro"], item["estimated_calls"] = 0, 0
    for fact in records:
        day = fact.at.astimezone(UTC).date().isoformat()
        row = daily_groups.setdefault(day, {"cost_micro": 0, "calls": 0})
        row["cost_micro"] += fact.cost_micro
        row["calls"] += fact.calls
        found = by_workflow.get(str(fact.workflow_id))
        if found and fact.estimated_calls:
            found["estimated_micro"] += fact.cost_micro
            found["estimated_calls"] += fact.estimated_calls
    from core.models import AccountingRecovery, ProviderCall

    pending = (
        await session.execute(
            select(ProviderCall.workflow_id, func.count())
            .join(AccountingRecovery, AccountingRecovery.call_id == ProviderCall.id)
            .where(
                ProviderCall.client_id.in_([uuid.UUID(identity) for identity in groups]),
                ProviderCall.period == period,
                AccountingRecovery.status != "complete",
            )
            .group_by(ProviderCall.workflow_id)
        )
    ).all()
    for item in items:
        item["recovery_pending"] = 0
    for workflow_id, count in pending:
        target = by_workflow.get(str(workflow_id))
        if target:
            target["recovery_pending"] = count
    return {
        **await governance_snapshot(session, [uuid.UUID(identity) for identity in groups]),
        "period": period,
        "clients": list(groups.values()),
        "workflows": items,
        "daily": [{"day": day, **values} for day, values in sorted(daily_groups.items())],
    }


@router.get("/admin/keys")
async def keys(
    session: DbSession, actor: Actor, workflow_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    query = select(VirtualKey).join(Workflow, Workflow.id == VirtualKey.workflow_id)
    query = query.where(Workflow.client_id.in_(permitted_clients(actor)))
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
    query = query.where(Workflow.client_id.in_(permitted_clients(actor)))
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
    from approvals.reviewers import require_reviewer

    workflow = await require_reviewer(session, actor, record.workflow_id)
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
    query = query.where(Alert.client_id.in_(permitted_clients(actor)))
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
    client_ids: set[uuid.UUID] = set((await session.execute(permitted_clients(actor))).scalars())
    workflow_ids = set(
        (
            await session.execute(select(Workflow.id).where(Workflow.client_id.in_(client_ids)))
        ).scalars()
    )
    return [
        {"scope": r.scope, "scope_id": r.scope_id, "active": r.active, "reason": r.reason}
        for r in rows
        if r.scope == "global"
        or (r.scope == "client" and r.scope_id in {str(c) for c in client_ids})
        or (r.scope == "workflow" and r.scope_id in {str(w) for w in workflow_ids})
    ]


@router.get("/admin/reconciliation")
async def reconciliation(session: DbSession, actor: Actor) -> dict[str, Any]:
    job_query = select(ReconcileJob).order_by(ReconcileJob.day.desc()).limit(90)
    snapshot_query = select(ReconcileSnapshot).order_by(ReconcileSnapshot.day.desc()).limit(1000)
    account_query = select(ProviderAccount)
    job_query = job_query.where(ReconcileJob.client_id.in_(permitted_clients(actor)))
    snapshot_query = snapshot_query.where(ReconcileSnapshot.client_id.in_(permitted_clients(actor)))
    account_query = account_query.where(ProviderAccount.client_id.in_(permitted_clients(actor)))
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
