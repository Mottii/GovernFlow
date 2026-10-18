import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from admin_api.app import app as admin_app
from audit.chain import verify_chain
from core.models import (
    AccountingReceipt,
    AccountingRecovery,
    Budget,
    ChargeEntry,
    ProviderCall,
    Reservation,
    Run,
)
from policy.accounting import Evidence, EvidenceConflict, apply, mark_dispatched
from policy.budget import reserve
from policy.journal import SQLiteJournal, persist
from policy.reap import reap_stale
from policy.recovery import import_job, retry_due
from policy.replay import enqueue, replay


async def pending_call(governed, amount=80, run_key="accounting"):
    async with governed.factory() as session:
        held = await reserve(
            session,
            governed.a.workflow.id,
            run_key,
            amount,
            100000,
            provider="openai",
            model="gpt-mock",
        )
        assert held is not None
        await mark_dispatched(session, held.id, uuid.uuid4())
        call = await session.get(ProviderCall, held.id)
        evidence = Evidence(
            evidence_id=uuid.uuid4(),
            agency_id=call.agency_id,
            client_id=call.client_id,
            call_id=call.id,
            kind="measured",
            amount_micro=15,
            reason="terminal_usage",
            usage={"input": 12, "output": 3},
        )
        return held, evidence


@pytest.mark.parametrize("reap_first", [True, False])
async def test_late_measurement_and_reaper_order_are_financially_identical(governed, reap_first):
    held, evidence = await pending_call(governed)
    async with governed.factory() as session:
        await session.execute(
            update(ProviderCall)
            .where(ProviderCall.id == held.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    await persist(evidence.model_dump(mode="json"))
    async with governed.factory() as session:
        if reap_first:
            assert await reap_stale(session, datetime.now(UTC)) == 1
        assert await replay(session) == 1
        assert await reap_stale(session, datetime.now(UTC)) == 0
        assert await replay(session) == 0
        call = await session.get(ProviderCall, held.id)
        assert call.state == "settled_measured" and call.charged_micro == 15
        budget = await session.get(Budget, held.budget_id)
        run = await session.get(Run, held.run_id)
        assert (budget.spent_micro, budget.reserved_micro, run.cost_micro, run.reserved_micro) == (
            15,
            0,
            15,
            0,
        )
        assert (await session.execute(select(func.sum(ChargeEntry.delta_micro)))).scalar_one() == 15
        assert await verify_chain(session, governed.a.client.id) == (True, None)
        receipts = (await session.execute(select(AccountingReceipt))).scalars().all()
        assert len(receipts) == (2 if reap_first else 1)


async def test_evidence_deduplication_conflict_and_audited_admin_correction(governed):
    held, evidence = await pending_call(governed)

    async def one():
        async with governed.factory() as session:
            result = await apply(session, evidence)
            await session.commit()
            return result

    assert sorted(await asyncio.gather(one(), one())) == ["applied", "duplicate"]
    async with governed.factory() as session:
        with pytest.raises(EvidenceConflict, match="reused"):
            await apply(session, evidence.model_copy(update={"amount_micro": 16}))
        await session.rollback()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        headers = {"authorization": "Bearer test-admin"}
        url = f"/admin/provider-calls/{held.id}/corrections"
        body = {
            "evidence_id": str(uuid.uuid4()),
            "expected_revision": 1,
            "measured_micro": 8,
            "reason": "Provider invoice",
            "evidence_ref": "invoice:fixture-42",
        }
        corrected = await admin.post(url, headers=headers, json=body)
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["charged_micro"] == 8
        assert (await admin.post(url, headers=headers, json=body)).status_code == 200
        assert (
            await admin.post(url, headers=headers, json={**body, "evidence_id": str(uuid.uuid4())})
        ).status_code == 409
        detail = await admin.get(f"/admin/provider-calls/{held.id}", headers=headers)
        assert [a["delta_micro"] for a in detail.json()["adjustments"]] == [15, -7]
        assert detail.json()["adjustments"][-1]["actor"] == "bootstrap admin"
        report = await admin.get(
            f"/admin/reports/{governed.a.client.id}/{datetime.now(UTC):%Y-%m}", headers=headers
        )
        assert report.json()["totals"]["cost_micro"] == 8
        assert report.json()["totals"]["calls"] == 1
        assert report.json()["totals"]["corrected_calls"] == 1


async def test_commit_before_journal_ack_is_verified_without_second_charge(governed, monkeypatch):
    held, evidence = await pending_call(governed)
    await persist(evidence.model_dump(mode="json"))
    original = SQLiteJournal.ack

    async def unavailable(*args, **kwargs):
        raise OSError("simulated lost acknowledgement")

    monkeypatch.setattr(SQLiteJournal, "ack", unavailable)
    async with governed.factory() as session:
        assert await replay(session) == 0
        call = await session.get(ProviderCall, held.id)
        assert call.charged_micro == 15
    monkeypatch.setattr(SQLiteJournal, "ack", original)
    with SQLiteJournal().connection() as connection:
        connection.execute("UPDATE evidence SET next_attempt=0")
    async with governed.factory() as session:
        assert await replay(session) == 1
        assert (
            await session.execute(select(func.count()).select_from(ChargeEntry))
        ).scalar_one() == 1
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15


async def test_journal_identity_is_immutable_and_conflicts_remain_quarantined(governed):
    held, evidence = await pending_call(governed)
    await persist(evidence.model_dump(mode="json"))
    with pytest.raises(EvidenceConflict):
        await persist(evidence.model_copy(update={"amount_micro": 99}).model_dump(mode="json"))
    async with governed.factory() as session:
        await apply(session, evidence)
        await session.commit()
    other = evidence.model_copy(update={"evidence_id": uuid.uuid4(), "amount_micro": 99})
    await persist(other.model_dump(mode="json"))
    async with governed.factory() as session:
        assert await replay(session) == 1
        job = await session.get(AccountingRecovery, (evidence.agency_id, other.evidence_id))
        assert job.status == "quarantined"
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15
    entries = [p async for _, p in SQLiteJournal().entries()]
    assert len(entries) == 1


async def test_postgres_job_can_retry_even_when_external_record_is_missing(governed):
    held, evidence = await pending_call(governed)
    async with governed.factory() as session:
        await import_job(session, evidence.model_dump(mode="json"))
        assert await retry_due(session) == 1
        assert await retry_due(session) == 0
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15


async def test_legacy_reaped_reservation_does_not_discard_queued_charge(governed):
    async with governed.factory() as session:
        budget = (
            await session.execute(
                select(Budget).where(Budget.workflow_id == governed.a.workflow.id)
            )
        ).scalar_one()
        run = Run(workflow_id=governed.a.workflow.id, external_run_id="legacy")
        session.add(run)
        await session.flush()
        reservation = Reservation(
            budget_id=budget.id, run_id=run.id, est_micro=80, status="released"
        )
        session.add(reservation)
        await session.commit()
        payload = {
            "agency_id": str(budget.agency_id),
            "client_id": str(governed.a.client.id),
            "workflow_id": str(governed.a.workflow.id),
            "provider": "openai",
            "model": "gpt-mock",
            "cost_micro": 15,
            "held": {
                "id": str(reservation.id),
                "budget_id": str(budget.id),
                "run_id": str(run.id),
                "est_micro": 80,
            },
        }
    await enqueue(payload)
    async with governed.factory() as session:
        assert await replay(session) == 1
        assert await replay(session) == 0
        assert (await session.get(Budget, budget.id)).spent_micro == 15
        assert (await session.get(ProviderCall, reservation.id)).charged_micro == 15


async def test_append_only_evidence_and_cross_revision_measurement_conflict(governed):
    from sqlalchemy.exc import DBAPIError

    held, evidence = await pending_call(governed)
    async with governed.factory() as session:
        await apply(session, evidence)
        await session.commit()
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(update(ChargeEntry).values(delta_micro=0))
        await session.rollback()
        with pytest.raises(EvidenceConflict, match="Conflicting complete"):
            await apply(
                session,
                evidence.model_copy(update={"evidence_id": uuid.uuid4(), "amount_micro": 2}),
            )


async def test_corrections_restate_original_month_without_changing_call_count(governed):
    from admin_api.reports import month_bounds, monthly

    held, evidence = await pending_call(governed)
    now = datetime.now(UTC)
    previous = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    start, _ = month_bounds(previous)
    async with governed.factory() as session:
        await session.execute(
            update(Budget).where(Budget.id == held.budget_id).values(period=previous)
        )
        await session.execute(
            update(ProviderCall)
            .where(ProviderCall.id == held.id)
            .values(period=previous, created_at=start)
        )
        estimate = evidence.model_copy(
            update={
                "evidence_id": uuid.uuid4(),
                "kind": "estimate",
                "amount_micro": 80,
                "usage": None,
            }
        )
        await apply(session, estimate)
        await session.commit()
        await apply(session, evidence)
        await session.commit()
        past = await monthly(session, governed.a.client.id, previous)
        current = await monthly(session, governed.a.client.id, f"{now:%Y-%m}")
        assert past["totals"]["cost_micro"] == 15 and past["totals"]["calls"] == 1
        assert past["totals"]["estimated_calls"] == 0
        assert current["totals"]["cost_micro"] == 0 and current["totals"]["calls"] == 0


async def test_concurrent_recovery_workers_apply_one_measurement(governed):
    from policy.recovery import process_job

    held, evidence = await pending_call(governed)
    async with governed.factory() as session:
        record = await import_job(session, evidence.model_dump(mode="json"))

    async def one():
        async with governed.factory() as session:
            return await process_job(session, record)

    await asyncio.gather(*(one() for _ in range(10)))
    async with governed.factory() as session:
        assert (await session.get(ProviderCall, held.id)).charged_micro == 15
        assert (
            await session.execute(select(func.count()).select_from(ChargeEntry))
        ).scalar_one() == 1
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15


async def test_mixed_reserve_settle_correct_reap_and_cap_change_do_not_deadlock(governed):
    from policy.budget import settle

    held, evidence = await pending_call(governed)
    async with governed.factory() as session:
        await session.execute(
            update(ProviderCall)
            .where(ProviderCall.id == held.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    async def ordinary(index):
        async with governed.factory() as session:
            reservation = await reserve(session, governed.a.workflow.id, f"mixed:{index}", 1, 1000)
            assert reservation is not None
            await settle(session, reservation, 1)

    async def measured():
        async with governed.factory() as session:
            await apply(session, evidence)
            await session.commit()

    async def reaper():
        async with governed.factory() as session:
            await reap_stale(session, datetime.now(UTC))

    async def cap_change():
        async with governed.factory() as session:
            await session.execute(
                update(Budget).where(Budget.id == held.budget_id).values(cap_micro=200)
            )
            await session.commit()

    async with asyncio.timeout(20):
        await asyncio.gather(measured(), reaper(), cap_change(), *(ordinary(i) for i in range(50)))
    async with governed.factory() as session:
        budget = await session.get(Budget, held.budget_id)
        assert (budget.spent_micro, budget.reserved_micro) == (65, 0)
        assert (await session.execute(select(func.sum(ChargeEntry.delta_micro)))).scalar_one() == 65
