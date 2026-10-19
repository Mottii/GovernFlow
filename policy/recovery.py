"""Leased accounting application; journal acknowledgement follows a committed receipt."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import set_agency_context
from core.models import (
    AccountingReceipt,
    AccountingRecovery,
    Budget,
    ProviderCall,
    Reservation,
    Run,
    Workflow,
)
from policy.accounting import Evidence, EvidenceConflict, apply, database_now, locked_call, register
from policy.journal import CallSeed, JournalRecord, encode, journal


async def ensure_seed(session: AsyncSession, record: JournalRecord) -> None:
    from policy.accounting import call_lock
    from policy.allocations import client_bucket

    await call_lock(session, record.call_id)
    seed = record.seed
    if seed is None:
        return
    call = await session.get(ProviderCall, record.call_id)
    if call:
        if (
            call.agency_id,
            call.client_id,
            call.budget_id,
            call.run_id,
            call.workflow_id,
            call.price_snapshot,
        ) != (
            record.agency_id,
            record.client_id,
            seed.budget_id,
            seed.run_id,
            seed.workflow_id,
            seed.pricing,
        ):
            raise EvidenceConflict("Recovery seed differs from original call")
        return
    # An outage call consumes no uncommitted reservation. Its cached parents must already
    # exist and match authenticated ownership; materialize under the budget lock once.
    workflow = await session.get(Workflow, seed.workflow_id)
    if workflow is None:
        raise EvidenceConflict("Cached workflow is unavailable")
    shared = await client_bucket(session, workflow, seed.period)
    budget = (
        await session.execute(select(Budget).where(Budget.id == seed.budget_id).with_for_update())
    ).scalar_one_or_none()
    if budget is None:
        raise EvidenceConflict("Original budget is unavailable")
    call = await session.get(ProviderCall, record.call_id)
    if call:
        await ensure_seed(session, record)
        return
    run = (
        await session.execute(select(Run).where(Run.id == seed.run_id).with_for_update())
    ).scalar_one_or_none()
    workflow = await session.get(Workflow, seed.workflow_id)
    if (
        run is None
        or workflow is None
        or workflow.client_id != record.client_id
        or workflow.agency_id != record.agency_id
        or budget.workflow_id != workflow.id
        or run.workflow_id != workflow.id
        or budget.period != seed.period
    ):
        raise EvidenceConflict("Cached call parents do not match evidence ownership")
    reservation = Reservation(
        id=record.call_id,
        client_budget_id=shared.id,
        budget_id=budget.id,
        run_id=run.id,
        est_micro=seed.estimate_micro,
        status="released",
        actual_micro=0,
    )
    session.add(reservation)
    await session.flush()
    call = await register(
        session,
        reservation,
        workflow.id,
        provider=seed.provider,
        model=seed.model,
        pricing=seed.pricing,
    )
    call.policy_version = seed.policy_version if seed.policy_version is not None else 0
    call.credential_version_id = seed.credential_version_id
    call.state, call.provenance = "dispatched", "outage"
    call.dispatch_at = datetime.now(UTC)
    call.lease_until = datetime.now(UTC)
    run.step_count += 1


async def import_job(session: AsyncSession, payload: dict[str, Any]) -> JournalRecord:
    record = JournalRecord.model_validate_json(encode(payload))
    if record.kind in ("correction", "legacy"):
        raise EvidenceConflict("Journal cannot authorize administrator corrections")
    if session.in_transaction():
        await session.rollback()
    await set_agency_context(session, record.agency_id)
    checksum = hashlib.sha256(encode(payload)).digest()
    await session.execute(
        insert(AccountingRecovery)
        .values(
            agency_id=record.agency_id,
            client_id=record.client_id,
            call_id=record.call_id,
            evidence_id=record.evidence_id,
            checksum=checksum,
            payload=payload,
        )
        .on_conflict_do_nothing()
    )
    job = await session.get(
        AccountingRecovery, (record.agency_id, record.evidence_id), populate_existing=True
    )
    assert job is not None
    if job.checksum != checksum:
        job.status, job.last_error = "quarantined", "evidence_identity_conflict"
        await session.commit()
        raise EvidenceConflict("Recovery evidence identity has conflicting payloads")
    await session.commit()
    return record


async def process_job(session: AsyncSession, record: JournalRecord) -> bool:
    now = await database_now(session)
    owner = uuid.uuid4()
    job = (
        await session.execute(
            select(AccountingRecovery)
            .where(
                AccountingRecovery.agency_id == record.agency_id,
                AccountingRecovery.evidence_id == record.evidence_id,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        await session.rollback()
        return False
    if job.status == "complete":
        receipt = await session.get(AccountingReceipt, (record.agency_id, record.evidence_id))
        valid = (
            receipt is not None
            and receipt.call_id == record.call_id
            and receipt.checksum == record.evidence().checksum()
        )
        await session.rollback()
        if not valid:
            raise EvidenceConflict("Completed recovery has no matching committed receipt")
        return True
    if (
        job.status == "quarantined"
        or job.next_attempt > now
        or (job.status == "running" and job.lease_until and job.lease_until > now)
    ):
        await session.rollback()
        return False
    job.status, job.lease_owner, job.lease_until = "running", owner, now + timedelta(seconds=60)
    job.attempts += 1
    await session.commit()
    try:
        job = (
            await session.execute(
                select(AccountingRecovery)
                .where(
                    AccountingRecovery.agency_id == record.agency_id,
                    AccountingRecovery.evidence_id == record.evidence_id,
                    AccountingRecovery.lease_owner == owner,
                    AccountingRecovery.lease_until > func.clock_timestamp(),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if job is None:
            await session.rollback()
            return False
        await ensure_seed(session, record)
        await apply(session, record.evidence(), actor="recovery")
        job.status, job.last_error, job.lease_owner, job.lease_until = "complete", None, None, None
        await session.commit()
        return True
    except (ValueError, DBAPIError, OSError, TimeoutError) as error:
        await session.rollback()
        job = await session.get(AccountingRecovery, (record.agency_id, record.evidence_id))
        if job and job.lease_owner == owner:
            permanent = isinstance(error, ValueError)
            job.status = "quarantined" if permanent or job.attempts >= 10 else "retry"
            job.last_error = "evidence_conflict" if permanent else "storage_unavailable"
            job.next_attempt = (await database_now(session)) + timedelta(
                seconds=min(900, 5 * 2 ** min(job.attempts, 8))
            )
            job.lease_owner, job.lease_until = None, None
            await session.commit()
        return False


async def recover_journal(session: AsyncSession) -> int:
    from policy.journal import S3Journal

    store = journal()
    try:
        return await _recover_store(session, store)
    finally:
        if isinstance(store, S3Journal):
            await asyncio.to_thread(store.client.close)


async def _recover_store(session: AsyncSession, store: Any) -> int:
    done = 0
    async for identity, payload in store.entries():
        try:
            record = await import_job(session, payload)
            if await process_job(session, record):
                await store.ack(identity, payload)
                done += 1
        except (ValueError, DBAPIError, OSError, TimeoutError) as error:
            await session.rollback()
            await store.failed(
                identity,
                "invalid_evidence" if isinstance(error, ValueError) else "storage_unavailable",
                quarantine=isinstance(error, ValueError),
            )
    return done


async def recover_legacy(session: AsyncSession) -> int:
    """Read old queue entries in place; preserve unrepresentable entries for inspection."""
    from core.models import DEFAULT_AGENCY_ID
    from policy.replay import connect

    def pending() -> list[tuple[str, str]]:
        with connect() as connection:
            return list(connection.execute("SELECT id,payload FROM queue"))

    rows = await asyncio.to_thread(pending)
    done = 0
    for identity, raw in rows:
        try:
            payload = json.loads(raw)
            if (
                not payload.get("ready", True)
                and datetime.now(UTC).timestamp() - payload["queued_at"] < 3600
            ):
                continue
            agency_id = uuid.UUID(payload.get("agency_id", str(DEFAULT_AGENCY_ID)))
            if session.in_transaction():
                await session.rollback()
            await set_agency_context(session, agency_id)
            old_receipt = await session.execute(
                text("SELECT 1 FROM replay_receipts WHERE id=:id AND agency_id=:agency"),
                {"id": uuid.UUID(identity), "agency": agency_id},
            )
            if old_receipt.scalar_one_or_none() is not None:
                held_payload = payload.get("held")
                existing = (
                    await session.get(Reservation, uuid.UUID(held_payload["id"]))
                    if held_payload
                    else None
                )
                if (
                    existing is None
                    or existing.status != "settled"
                    or existing.actual_micro != payload["cost_micro"]
                ):
                    raise EvidenceConflict("Legacy receipt lacks a matching durable settlement")
            held = payload.get("held")
            if held:
                call = await locked_call(session, uuid.UUID(held["id"]))
                if call.budget_id != uuid.UUID(held["budget_id"]) or call.run_id != uuid.UUID(
                    held["run_id"]
                ):
                    raise EvidenceConflict("Legacy reservation parents do not match")
                if call.provider is None:
                    call.provider, call.model = payload["provider"], payload["model"]
                call_id = call.id
            else:
                workflow_id = uuid.UUID(payload["workflow_id"])
                workflow = await session.get(Workflow, workflow_id)
                if workflow is None or workflow.client_id != uuid.UUID(payload["client_id"]):
                    raise EvidenceConflict("Legacy workflow ownership is unavailable")
                budget = (
                    await session.execute(
                        select(Budget)
                        .where(
                            Budget.workflow_id == workflow_id, Budget.period == payload["period"]
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                await session.execute(
                    insert(Run)
                    .values(workflow_id=workflow_id, external_run_id=payload["run_key"])
                    .on_conflict_do_nothing(index_elements=[Run.workflow_id, Run.external_run_id])
                )
                run = (
                    await session.execute(
                        select(Run).where(
                            Run.workflow_id == workflow_id,
                            Run.external_run_id == payload["run_key"],
                        )
                    )
                ).scalar_one()
                call_id = uuid.UUID(identity)
                record = JournalRecord(
                    evidence_id=uuid.UUID(identity),
                    agency_id=agency_id,
                    client_id=workflow.client_id,
                    call_id=call_id,
                    kind="estimate",
                    amount_micro=max(1, payload["cost_micro"]),
                    reason="legacy_outage",
                    seed=CallSeed(
                        budget_id=budget.id,
                        run_id=run.id,
                        workflow_id=workflow_id,
                        period=budget.period,
                        provider=payload["provider"],
                        model=payload["model"],
                        estimate_micro=max(1, payload["cost_micro"]),
                        pricing={},
                    ),
                )
                await ensure_seed(session, record)
            if payload["cost_micro"] == 0:
                raise EvidenceConflict("Legacy zero charge lacks proof of non-dispatch")
            evidence = Evidence(
                evidence_id=uuid.UUID(identity),
                agency_id=agency_id,
                client_id=uuid.UUID(payload["client_id"]),
                call_id=call_id,
                kind="measured" if payload.get("estimated") is False else "legacy",
                amount_micro=payload["cost_micro"],
                reason="legacy_recovery",
            )
            await apply(session, evidence, actor="legacy_recovery", allow_legacy=True)
            await session.commit()

            def remove(queue_id: str = identity) -> None:
                with connect() as connection:
                    connection.execute("DELETE FROM queue WHERE id=?", (queue_id,))

            await asyncio.to_thread(remove)
            done += 1
        except (ValueError, KeyError, TypeError, DBAPIError, OSError):
            await session.rollback()
            continue  # The raw legacy entry remains intact.
    return done


async def retry_due(session: AsyncSession) -> int:
    keys = (
        await session.execute(
            select(AccountingRecovery.agency_id, AccountingRecovery.evidence_id)
            .where(
                AccountingRecovery.status.in_(("pending", "retry", "running")),
                AccountingRecovery.next_attempt <= func.clock_timestamp(),
                or_(
                    AccountingRecovery.lease_until.is_(None),
                    AccountingRecovery.lease_until <= func.clock_timestamp(),
                ),
            )
            .limit(100)
        )
    ).all()
    await session.rollback()
    done = 0
    for agency_id, evidence_id in keys:
        await set_agency_context(session, agency_id)
        job = await session.get(AccountingRecovery, (agency_id, evidence_id))
        assert job is not None
        record = JournalRecord.model_validate_json(encode(job.payload))
        await session.rollback()
        done += int(await process_job(session, record))
    return done
