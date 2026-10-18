"""One transactional charge path for live calls, recovery, leases, and corrections."""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.models import (
    AccountingReceipt,
    Budget,
    ChargeEntry,
    ProviderCall,
    Reservation,
    Run,
    Workflow,
)

MAX_MONEY = 2**63 - 1


class EvidenceConflict(ValueError):
    pass


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    evidence_id: uuid.UUID
    agency_id: uuid.UUID
    client_id: uuid.UUID
    call_id: uuid.UUID
    kind: Literal["measured", "estimate", "void", "correction", "legacy"]
    amount_micro: int = Field(ge=0, le=MAX_MONEY)
    reason: str = Field(min_length=1, max_length=200)
    usage: dict[str, Any] | None = None
    provider_request_id: str | None = Field(default=None, max_length=200)
    expected_revision: int | None = Field(default=None, ge=0)
    evidence_ref: str | None = Field(default=None, max_length=2000)

    @field_validator("usage")
    @classmethod
    def usage_counters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            allowed = {
                "input",
                "output",
                "cache_read",
                "cache_write",
                "cache_write_hour",
                "reasoning",
            }
            if set(value) - allowed or any(
                isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_MONEY
                for n in value.values()
            ):
                raise ValueError(
                    "Only nonnegative normalized usage counters may enter accounting evidence"
                )
        return value

    def checksum(self) -> bytes:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).digest()


def deadline(at: datetime) -> datetime:
    return at + timedelta(seconds=int(os.environ.get("CALL_LEASE_SECONDS", "60")))


def price_snapshot(price: Any) -> dict[str, Any]:
    result = {
        name: getattr(price, name)
        for name in (
            "input",
            "output",
            "cache_read",
            "cache_write",
            "cache_write_hour",
            "reasoning",
        )
    }
    meter = price.meter or {}
    result["meter"] = {"standard_only": bool(meter.get("standard_only", False))}
    tier = meter.get("long_context")
    if tier:
        result["meter"]["long_context"] = {
            key: tier[key] for key in ("threshold_tokens", "input_multiplier", "output_multiplier")
        }
    return result


async def database_now(session: AsyncSession) -> datetime:
    value: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    return value


async def register(
    session: AsyncSession,
    record: Reservation,
    workflow_id: uuid.UUID,
    *,
    provider: str | None = None,
    model: str | None = None,
    pricing: dict[str, Any] | None = None,
    legacy: bool = False,
) -> ProviderCall:
    workflow = await session.get(Workflow, workflow_id)
    budget = await session.get(Budget, record.budget_id)
    assert workflow is not None and budget is not None
    state = "dispatched" if legacy and record.status == "reserved" else "reserved"
    amount = record.actual_micro or 0 if record.status == "settled" else 0
    if record.status == "settled":
        state = "settled_estimated"
    elif record.status == "released":
        state = "voided"
    call = ProviderCall(
        id=record.id,
        reservation_id=record.id,
        agency_id=workflow.agency_id,
        client_id=workflow.client_id,
        workflow_id=workflow.id,
        budget_id=budget.id,
        run_id=record.run_id,
        period=budget.period,
        provider=provider,
        model=model,
        estimate_micro=record.est_micro,
        charged_micro=amount,
        legacy_baseline_micro=amount if legacy and record.status == "settled" else None,
        created_at=record.created_at,
        state=state,
        revision=0,
        lease_until=deadline(await database_now(session)),
        price_snapshot=pricing or {},
        provenance="legacy_" + record.status if legacy else "gateway",
        uncertainty_reason="legacy_dispatch_unknown" if legacy else None,
    )
    session.add(call)
    await session.flush()
    return call


async def locked_call(session: AsyncSession, call_id: uuid.UUID) -> ProviderCall:
    call = (
        await session.execute(
            select(ProviderCall)
            .where(ProviderCall.id == call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if call is None:
        record = (
            await session.execute(
                select(Reservation).where(Reservation.id == call_id).with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            raise EvidenceConflict("Call or reservation is unavailable")
        # A second adopter may have waited for this same reservation.
        call = (
            await session.execute(
                select(ProviderCall).where(ProviderCall.id == call_id).with_for_update()
            )
        ).scalar_one_or_none()
        if call is None:
            budget = await session.get(Budget, record.budget_id)
            assert budget is not None
            call = await register(session, record, budget.workflow_id, legacy=True)
    return call


async def mark_dispatched(session: AsyncSession, call_id: uuid.UUID, owner: uuid.UUID) -> None:
    call = await locked_call(session, call_id)
    if call.state != "reserved":
        raise EvidenceConflict("A call may be dispatched only once")
    call.state, call.dispatch_at = "dispatched", await database_now(session)
    call.lease_owner, call.lease_until = owner, deadline(call.dispatch_at)
    await session.commit()


async def renew(session: AsyncSession, call_id: uuid.UUID, owner: uuid.UUID) -> bool:
    result = await session.execute(
        update(ProviderCall)
        .where(
            ProviderCall.id == call_id,
            ProviderCall.state == "dispatched",
            ProviderCall.lease_owner == owner,
            ProviderCall.lease_until > func.clock_timestamp(),
        )
        .values(lease_until=deadline(await database_now(session)))
        .returning(ProviderCall.id)
    )
    await session.commit()
    return result.scalar_one_or_none() is not None


async def apply(
    session: AsyncSession,
    evidence: Evidence,
    *,
    actor: str = "gateway",
    allow_correction: bool = False,
    allow_legacy: bool = False,
) -> str:
    """Caller commits; receipt, balances, ledger, and audit share that commit."""
    if evidence.kind == "legacy" and not allow_legacy:
        raise EvidenceConflict("Legacy evidence requires the upgrade adapter")
    if evidence.kind == "correction" and not allow_correction:
        raise EvidenceConflict("Administrator correction requires authenticated authority")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"accounting:{evidence.agency_id}:{evidence.evidence_id}"},
    )
    checksum = evidence.checksum()
    receipt = await session.get(AccountingReceipt, (evidence.agency_id, evidence.evidence_id))
    if receipt:
        if receipt.checksum != checksum or receipt.call_id != evidence.call_id:
            raise EvidenceConflict("Evidence identity was reused with different content")
        return "duplicate"
    call = await locked_call(session, evidence.call_id)
    if (call.agency_id, call.client_id) != (evidence.agency_id, evidence.client_id):
        raise EvidenceConflict("Evidence ownership does not match call")
    if (
        call.provider_request_id
        and evidence.provider_request_id
        and call.provider_request_id != evidence.provider_request_id
    ):
        raise EvidenceConflict("Provider request identity does not match call")
    if evidence.expected_revision is not None and call.revision != evidence.expected_revision:
        raise EvidenceConflict("Call revision changed")
    disposition = "applied"
    if evidence.kind in ("estimate", "legacy") and call.state in ("settled_measured", "voided"):
        if call.provenance != "legacy_released":
            disposition = "superseded"
    if (
        evidence.kind == "estimate"
        and call.state == "settled_estimated"
        and max(call.estimate_micro, call.charged_micro, evidence.amount_micro)
        == call.charged_micro
    ):
        disposition = "duplicate_estimate"
    if evidence.kind == "measured" and (
        call.state == "settled_measured" or call.provenance == "admin"
    ):
        if call.charged_micro != evidence.amount_micro or (
            call.usage is not None and evidence.usage is not None and call.usage != evidence.usage
        ):
            raise EvidenceConflict(
                "Conflicting complete measurement requires administrator resolution"
            )
        disposition = "duplicate_measurement"
    if (
        evidence.kind == "measured"
        and call.state == "voided"
        and call.provenance != "legacy_released"
    ):
        raise EvidenceConflict("Measurement contradicts non-dispatch evidence")
    if evidence.kind == "void" and (evidence.amount_micro != 0 or call.state == "settled_measured"):
        raise EvidenceConflict("Void contradicts a measured charge")
    if evidence.kind == "correction" and (
        call.state not in ("settled_estimated", "settled_measured")
        or evidence.expected_revision is None
        or not evidence.evidence_ref
    ):
        raise EvidenceConflict(
            "Corrections require a settled call, revision, and evidence reference"
        )
    if disposition == "applied":
        amount = (
            max(call.estimate_micro, call.charged_micro, evidence.amount_micro)
            if evidence.kind == "estimate"
            else evidence.amount_micro
        )
        reservation = (
            await session.execute(
                select(Reservation).where(Reservation.id == call.reservation_id).with_for_update()
            )
        ).scalar_one()
        budget = (
            await session.execute(
                select(Budget).where(Budget.id == call.budget_id).with_for_update()
            )
        ).scalar_one()
        run = (
            await session.execute(select(Run).where(Run.id == call.run_id).with_for_update())
        ).scalar_one()
        held = reservation.est_micro if reservation.status == "reserved" else 0
        delta = amount - call.charged_micro
        if budget.reserved_micro < held or run.reserved_micro < held:
            raise EvidenceConflict("Reservation balances require operator reconciliation")
        if (
            not 0 <= budget.spent_micro + delta <= MAX_MONEY
            or not 0 <= run.cost_micro + delta <= MAX_MONEY
        ):
            raise EvidenceConflict("Charge exceeds representable balances")
        budget.reserved_micro -= held
        budget.spent_micro += delta
        run.reserved_micro -= held
        run.cost_micro += delta
        previous_revision = call.revision
        call.revision += 1
        call.charged_micro = amount
        call.state = {
            "measured": "settled_measured",
            "estimate": "settled_estimated",
            "void": "voided",
            "correction": "settled_measured",
            "legacy": "settled_estimated",
        }[evidence.kind]
        call.uncertainty_reason = (
            evidence.reason if evidence.kind in ("estimate", "legacy") else None
        )
        call.updated_at = await database_now(session)
        if evidence.usage is not None and evidence.kind != "estimate":
            call.usage = evidence.usage
        if evidence.provider_request_id:
            call.provider_request_id = evidence.provider_request_id
        if evidence.kind == "correction":
            call.provenance = "admin"
        reservation.status = "released" if call.state == "voided" else "settled"
        reservation.actual_micro, reservation.settled_at = amount, datetime.now(UTC)
        session.add(
            ChargeEntry(
                agency_id=call.agency_id,
                call_id=call.id,
                evidence_id=evidence.evidence_id,
                revision=call.revision,
                delta_micro=delta,
                amount_micro=amount,
                kind=evidence.kind,
                actor=actor,
                evidence_ref=evidence.evidence_ref,
                reason=evidence.reason,
            )
        )
        await append_event(
            session,
            AuditEventInput(
                call.client_id,
                "provider_call_corrected"
                if previous_revision or call.legacy_baseline_micro is not None
                else "provider_error"
                if evidence.kind == "void"
                else "provider_call",
                call.provider,
                call.model,
                usage={
                    "call_id": str(call.id),
                    "period": call.period,
                    "reason": evidence.reason,
                    "revision": call.revision,
                    "usage": evidence.usage,
                },
                cost_micro=delta,
                estimated=call.state == "settled_estimated",
                content_ref="provider-call:" + str(call.id),
            ),
            call.run_id,
        )
        if delta > 0 and (
            amount > call.estimate_micro
            or budget.spent_micro + budget.reserved_micro > budget.cap_micro
        ):
            from policy.loop import trip

            await trip(
                session, call.workflow_id, "budget_overrun", run_id=call.run_id, cost_micro=delta
            )
    session.add(
        AccountingReceipt(
            agency_id=evidence.agency_id,
            evidence_id=evidence.evidence_id,
            call_id=evidence.call_id,
            checksum=checksum,
            disposition=disposition,
        )
    )
    await session.flush()
    return disposition
