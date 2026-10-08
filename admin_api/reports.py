import csv
import html
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from string import Template
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.auth import Actor, DbSession, client_scope, permit
from audit.chain import AuditEventInput, append_event
from core.models import Alert, Budget, Client, Event, MonthlyMetric, ReconcileJob, Run, Workflow

router = APIRouter()


def month_bounds(period: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
        if start.strftime("%Y-%m") != period or start.year >= 9999:
            raise ValueError("month")
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    except ValueError as exc:
        raise HTTPException(400, "Month must be YYYY-MM") from exc
    return start, end


def usd(value: int) -> str:
    return f"{Decimal(value) / 1000000:.6f}"


def hours(value: int) -> str:
    return f"{Decimal(value) / 60:.6f}"


async def monthly(session: AsyncSession, client_id: uuid.UUID, period: str) -> dict[str, Any]:
    start, end = month_bounds(period)
    client = await session.get(Client, client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    rows = (
        await session.execute(
            select(Workflow, Budget)
            .outerjoin(Budget, (Budget.workflow_id == Workflow.id) & (Budget.period == period))
            .where(Workflow.client_id == client_id)
            .order_by(Workflow.name)
        )
    ).all()
    workflows: dict[uuid.UUID | None, dict[str, Any]] = {}
    for workflow, budget in rows:
        workflows[workflow.id] = {
            "id": str(workflow.id),
            "name": workflow.name,
            "risk_class": workflow.risk_class,
            "cost_micro": 0,
            "calls": 0,
            "estimated_calls": 0,
            "cap_micro": budget.cap_micro if budget else None,
            "reserved_micro": budget.reserved_micro if budget else 0,
            "budget_spent_micro": budget.spent_micro if budget else 0,
        }
    events = (
        await session.execute(
            select(Event.cost_micro, Event.estimated, Run.workflow_id)
            .outerjoin(Run, Run.id == Event.run_id)
            .where(
                Event.client_id == client_id,
                Event.type == "provider_call",
                Event.ts >= start,
                Event.ts < end,
            )
        )
    ).all()
    for cost, estimated, workflow_id in events:
        group = workflows.get(workflow_id)
        if group is None:
            group = workflows.setdefault(
                None,
                {
                    "id": None,
                    "name": "Unassigned audit events",
                    "risk_class": "",
                    "cost_micro": 0,
                    "calls": 0,
                    "estimated_calls": 0,
                    "cap_micro": None,
                    "reserved_micro": 0,
                    "budget_spent_micro": 0,
                },
            )
        group["cost_micro"] += cost or 0
        group["calls"] += 1
        group["estimated_calls"] += int(estimated)
    metric = await session.get(MonthlyMetric, (client_id, period))
    jobs = (
        (
            await session.execute(
                select(ReconcileJob)
                .where(
                    ReconcileJob.client_id == client_id,
                    ReconcileJob.day >= start.date(),
                    ReconcileJob.day < end.date(),
                )
                .order_by(ReconcileJob.day.desc())
            )
        )
        .scalars()
        .all()
    )
    alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.client_id == client_id,
                    Alert.kind.in_(("possible_bypass", "reconciliation_drift")),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "client": {"id": str(client.id), "name": client.name},
        "period": period,
        "timezone": "UTC",
        "generated_at": datetime.now(UTC).isoformat(),
        "workflows": list(workflows.values()),
        "totals": {
            field: sum(row[field] for row in workflows.values())
            for field in (
                "cost_micro",
                "calls",
                "estimated_calls",
                "reserved_micro",
                "budget_spent_micro",
            )
        },
        "hours_saved": {
            "minutes": metric.minutes_saved,
            "hours": hours(metric.minutes_saved),
            "basis": metric.basis,
            "note": metric.note,
            "updated_by": metric.updated_by,
        }
        if metric
        else None,
        "reporting": {
            "jobs": [
                {"provider": job.provider, "day": str(job.day), "status": job.status}
                for job in jobs
            ],
            "open_drift": [
                {"kind": alert.kind, "details": alert.details}
                for alert in alerts
                if not alert.details.get("resolved")
                and alert.details.get("day", "").startswith(period)
            ],
            "gemini": "gateway-observed in v1",
        },
    }


def safe_cell(value: str) -> str:
    # Prevent spreadsheet formulas from tenant-controlled names and notes.
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
        else value
    )


def csv_report(report: dict[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "client",
            "period_utc",
            "workflow",
            "risk_class",
            "calls",
            "estimated_calls",
            "cost_micro_usd",
            "cost_usd",
            "reserved_micro_usd",
            "budget_spent_micro_usd",
            "minutes_saved",
            "hours_saved",
            "savings_basis",
            "savings_note",
        ]
    )
    prefix = [safe_cell(report["client"]["name"]), report["period"]]
    for row in report["workflows"]:
        writer.writerow(
            prefix
            + [
                safe_cell(row["name"]),
                row["risk_class"],
                row["calls"],
                row["estimated_calls"],
                row["cost_micro"],
                usd(row["cost_micro"]),
                row["reserved_micro"],
                row["budget_spent_micro"],
                "",
                "",
                "",
                "",
            ]
        )
    totals, saved = report["totals"], report["hours_saved"]
    writer.writerow(
        prefix
        + [
            "TOTAL",
            "",
            totals["calls"],
            totals["estimated_calls"],
            totals["cost_micro"],
            usd(totals["cost_micro"]),
            totals["reserved_micro"],
            totals["budget_spent_micro"],
            saved["minutes"] if saved else "",
            saved["hours"] if saved else "",
            saved["basis"] if saved else "",
            safe_cell(saved["note"]) if saved else "",
        ]
    )
    return buffer.getvalue()


def html_report(report: dict[str, Any]) -> str:
    escape = html.escape
    totals, saved = report["totals"], report["hours_saved"]
    saved_text = f"{saved['hours']} hours ({saved['basis']})" if saved else "Not supplied"
    rows = "".join(
        f"<tr><td>{escape(row['name'])}</td><td>{row['risk_class']}</td><td>{row['calls']}</td>"
        f"<td>{row['estimated_calls']}</td><td>${usd(row['cost_micro'])}</td></tr>"
        for row in report["workflows"]
    )
    jobs = report["reporting"]["jobs"]
    reporting = (
        f"{len(jobs)} daily job results; {sum(j['status'] == 'failed' for j in jobs)} failed."
        if jobs
        else "No provider reporting results for this month."
    )
    flags = len(report["reporting"]["open_drift"])
    template = Template(Path(__file__).with_name("templates").joinpath("monthly.html").read_text())
    return template.substitute(
        period=report["period"],
        client_name=escape(report["client"]["name"]),
        total_cost=usd(totals["cost_micro"]),
        calls=totals["calls"],
        estimated_calls=totals["estimated_calls"],
        saved_text=escape(saved_text),
        rows=rows,
        savings_note=escape(saved["note"]) if saved else "Supply savings evidence before sharing.",
        reporting=reporting,
        flags=flags,
        budget_spent=usd(totals["budget_spent_micro"]),
        reserved=usd(totals["reserved_micro"]),
        generated_at=escape(report["generated_at"]),
    )


class Savings(BaseModel):
    minutes_saved: int = Field(ge=0, le=10**9)
    basis: Literal["measured", "estimate"]
    note: str = Field(default="", max_length=2000)


@router.post("/admin/reports/{client_id}/{period}/hours")
async def set_hours(
    client_id: uuid.UUID, period: str, body: Savings, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    await client_scope(session, actor, client_id)
    month_bounds(period)
    if await session.get(Client, client_id) is None:
        raise HTTPException(404, "Client not found")
    values = {**body.model_dump(), "updated_by": actor.email, "updated_at": datetime.now(UTC)}
    await session.execute(
        insert(MonthlyMetric)
        .values(client_id=client_id, period=period, **values)
        .on_conflict_do_update(
            index_elements=[MonthlyMetric.client_id, MonthlyMetric.period], set_=values
        )
    )
    await append_event(
        session,
        AuditEventInput(
            client_id,
            "hours_saved_updated",
            usage={
                "period": period,
                "minutes_saved": body.minutes_saved,
                "basis": body.basis,
                "actor": actor.email,
            },
        ),
    )
    await session.commit()
    return {"minutes": body.minutes_saved, "hours": hours(body.minutes_saved), "basis": body.basis}


@router.get("/admin/reports/{client_id}/{period}")
async def get_report(
    client_id: uuid.UUID,
    period: str,
    session: DbSession,
    actor: Actor,
    format: Literal["json", "csv", "html"] = "json",
) -> Any:
    await client_scope(session, actor, client_id)
    report = await monthly(session, client_id, period)
    filename = f"governed-{client_id}-{period}"
    if format == "csv":
        return Response(
            csv_report(report),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )
    if format == "html":
        return Response(
            html_report(report),
            media_type="text/html",
            headers={
                "Content-Disposition": f'inline; filename="{filename}.html"',
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )
    return report
