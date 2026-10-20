"""Scoped call inspection and explicit evidence-backed administrator corrections."""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from admin_api.auth import Actor, DbSession, client_scope, permit, permitted_clients
from core.models import AccountingRecovery, ChargeEntry, ProviderCall
from policy.accounting import Evidence, EvidenceConflict, apply, database_now

router = APIRouter()


def view(call: ProviderCall) -> dict[str, Any]:
    return {
        "id": str(call.id),
        "client_id": str(call.client_id),
        "workflow_id": str(call.workflow_id),
        "client_budget_id": str(call.client_budget_id) if call.client_budget_id else None,
        "policy_version": call.policy_version,
        "credential_version_id": str(call.credential_version_id)
        if call.credential_version_id
        else None,
        "run_id": str(call.run_id),
        "state": call.state,
        "provider": call.provider,
        "model": call.model,
        "period": call.period,
        "estimate_micro": call.estimate_micro,
        "charged_micro": call.charged_micro,
        "revision": call.revision,
        "provenance": call.provenance,
        "uncertainty_reason": call.uncertainty_reason,
        "provider_request_id": call.provider_request_id,
        "usage": call.usage,
        "dispatch_at": call.dispatch_at.isoformat() if call.dispatch_at else None,
        "created_at": call.created_at.isoformat(),
        "updated_at": call.updated_at.isoformat(),
    }


async def scoped_call(session: DbSession, actor: Actor, identity: uuid.UUID) -> ProviderCall:
    call = (
        await session.execute(
            select(ProviderCall).where(
                ProviderCall.id == identity,
                ProviderCall.agency_id == actor.agency_id,
                ProviderCall.client_id.in_(permitted_clients(actor)),
            )
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(404, "Provider call not found")
    return call


@router.get("/admin/provider-calls")
async def calls(
    session: DbSession,
    actor: Actor,
    response: Response,
    client_id: uuid.UUID | None = None,
    workflow_id: uuid.UUID | None = None,
    state: str | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    query = select(ProviderCall).where(
        ProviderCall.agency_id == actor.agency_id,
        ProviderCall.client_id.in_(permitted_clients(actor)),
    )
    if client_id:
        await client_scope(session, actor, client_id)
        query = query.where(ProviderCall.client_id == client_id)
    if workflow_id:
        from admin_api.auth import workflow_scope

        await workflow_scope(session, actor, workflow_id)
        query = query.where(ProviderCall.workflow_id == workflow_id)
    if state:
        query = query.where(ProviderCall.state == state)
    if cursor:
        query = query.where(ProviderCall.id > cursor)
    rows = list((await session.execute(query.order_by(ProviderCall.id).limit(limit + 1))).scalars())
    if len(rows) > limit:
        response.headers["X-Next-Cursor"] = str(rows[limit - 1].id)
    return [view(call) for call in rows[:limit]]


@router.get("/admin/provider-calls/{call_id}")
async def call_detail(call_id: uuid.UUID, session: DbSession, actor: Actor) -> dict[str, Any]:
    call = await scoped_call(session, actor, call_id)
    entries = (
        await session.execute(
            select(ChargeEntry)
            .where(ChargeEntry.call_id == call_id, ChargeEntry.agency_id == actor.agency_id)
            .order_by(ChargeEntry.revision)
        )
    ).scalars()
    jobs = (
        await session.execute(
            select(AccountingRecovery).where(
                AccountingRecovery.call_id == call_id,
                AccountingRecovery.agency_id == actor.agency_id,
            )
        )
    ).scalars()
    return {
        **view(call),
        "adjustments": [
            {
                "evidence_id": str(e.evidence_id),
                "revision": e.revision,
                "delta_micro": e.delta_micro,
                "amount_micro": e.amount_micro,
                "kind": e.kind,
                "actor": e.actor,
                "reason": e.reason,
                "evidence_ref": e.evidence_ref,
            }
            for e in entries
        ],
        "recovery": [
            {
                "evidence_id": str(j.evidence_id),
                "status": j.status,
                "attempts": j.attempts,
                "last_error": j.last_error,
            }
            for j in jobs
        ],
    }


class Correction(BaseModel):
    evidence_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    measured_micro: int = Field(ge=0, le=2**63 - 1, strict=True)
    reason: str = Field(min_length=1, max_length=200)
    evidence_ref: str = Field(min_length=1, max_length=2000)


@router.post("/admin/provider-calls/{call_id}/corrections")
async def correct(
    call_id: uuid.UUID, body: Correction, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin")
    call = await scoped_call(session, actor, call_id)
    evidence = Evidence(
        evidence_id=body.evidence_id,
        agency_id=actor.agency_id,
        client_id=call.client_id,
        call_id=call.id,
        kind="correction",
        amount_micro=body.measured_micro,
        reason=body.reason,
        evidence_ref=body.evidence_ref,
        expected_revision=body.expected_revision,
    )
    try:
        await apply(session, evidence, actor=actor.email, allow_correction=True)
        await session.commit()
    except EvidenceConflict as error:
        await session.rollback()
        raise HTTPException(409, str(error)) from error
    await session.refresh(call)
    return view(call)


@router.get("/admin/accounting-recovery")
async def recovery(
    session: DbSession,
    actor: Actor,
    response: Response,
    client_id: uuid.UUID | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    permit(actor, "admin", "operator")
    query = select(AccountingRecovery).where(
        AccountingRecovery.agency_id == actor.agency_id,
        AccountingRecovery.client_id.in_(permitted_clients(actor)),
    )
    if client_id:
        await client_scope(session, actor, client_id)
        query = query.where(AccountingRecovery.client_id == client_id)
    if cursor:
        query = query.where(AccountingRecovery.evidence_id > cursor)
    jobs = list(
        (
            await session.execute(query.order_by(AccountingRecovery.evidence_id).limit(limit + 1))
        ).scalars()
    )
    if len(jobs) > limit:
        response.headers["X-Next-Cursor"] = str(jobs[limit - 1].evidence_id)
    return [
        {
            "evidence_id": str(j.evidence_id),
            "call_id": str(j.call_id),
            "client_id": str(j.client_id),
            "status": j.status,
            "attempts": j.attempts,
            "last_error": j.last_error,
            "next_attempt": j.next_attempt.isoformat(),
        }
        for j in jobs[:limit]
    ]


@router.post("/admin/accounting-recovery/{evidence_id}/retry")
async def retry(evidence_id: uuid.UUID, session: DbSession, actor: Actor) -> dict[str, str]:
    permit(actor, "admin", "operator")
    job = (
        await session.execute(
            select(AccountingRecovery)
            .where(
                AccountingRecovery.agency_id == actor.agency_id,
                AccountingRecovery.evidence_id == evidence_id,
                AccountingRecovery.client_id.in_(permitted_clients(actor)),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Recovery evidence not found")
    now = await database_now(session)
    if job.status == "complete" or (
        job.status == "running" and job.lease_until and job.lease_until > now
    ):
        raise HTTPException(409, "Recovery is complete or currently leased")
    job.status, job.next_attempt, job.attempts, job.last_error = (
        "pending",
        now,
        0,
        None,
    )
    await session.commit()
    return {"status": "pending"}
