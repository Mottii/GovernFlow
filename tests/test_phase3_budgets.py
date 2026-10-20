import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update

from admin_api.app import app as admin_app
from core.models import Budget, ClientBudget, Run, Workflow
from policy.allocations import client_bucket, renew_current
from policy.budget import reserve, settle

pytestmark = pytest.mark.asyncio


async def test_shared_cap_counts_existing_spend_and_200_cross_workflow_reservations(governed):
    async with governed.factory() as session:
        workflow = Workflow(
            client_id=governed.a.client.id,
            name="Sibling",
            risk_class="R1",
            policy={"models": ["*"]},
        )
        session.add(workflow)
        await session.flush()
        session.add(
            Budget(workflow_id=workflow.id, period=f"{datetime.now(UTC):%Y-%m}", cap_micro=10000)
        )
        await session.commit()
        sibling = workflow.id
        held = await reserve(session, governed.a.workflow.id, "before-cap", 5, 1000)
        await settle(session, held, 3)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://admin",
        headers={"authorization": "Bearer test-admin"},
    ) as admin:
        result = await admin.put(
            f"/admin/clients/{governed.a.client.id}/budget",
            json={
                "period": f"{datetime.now(UTC):%Y-%m}",
                "cap_micro": 53,
                "expected_revision": 1,
                "reason": "Shared allowance",
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["spent_micro"] == 3

    async def attempt(index):
        async with governed.factory() as session:
            return await reserve(
                session, sibling if index % 2 else governed.a.workflow.id, f"run-{index}", 1, 1000
            )

    async with asyncio.timeout(30):
        accepted = await asyncio.gather(*(attempt(i) for i in range(200)))
    assert sum(h is not None for h in accepted) == 50
    async with governed.factory() as session:
        bucket = (
            await session.execute(
                select(ClientBudget).where(ClientBudget.client_id == governed.a.client.id)
            )
        ).scalar_one()
        assert (bucket.spent_micro, bucket.reserved_micro) == (3, 50)


async def test_monthly_templates_are_idempotent_and_keep_manual_caps(governed, monkeypatch):
    period = f"{datetime.now(UTC):%Y-%m}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://admin",
        headers={"authorization": "Bearer test-admin"},
    ) as admin:
        for scope, identity in [
            ("client", governed.a.client.id),
            ("workflow", governed.a.workflow.id),
        ]:
            response = await admin.post(
                "/admin/budget-templates",
                json={
                    "scope": scope,
                    "scope_id": str(identity),
                    "cap_micro": 90,
                    "enabled": True,
                    "expected_revision": 0,
                    "effective_period": period,
                    "reason": "Renew monthly",
                },
            )
            assert response.status_code == 201, response.text
    async with governed.factory() as session:
        await session.execute(
            update(Budget).where(Budget.workflow_id == governed.a.workflow.id).values(cap_micro=70)
        )
        await session.commit()

    async def renew():
        async with governed.factory() as session:
            await renew_current(session)

    await asyncio.gather(*(renew() for _ in range(5)))
    async with governed.factory() as session:
        workflow = await session.get(Workflow, governed.a.workflow.id)
        shared = await client_bucket(session, workflow, period)
        assert shared.cap_micro == 90
        budget = (
            await session.execute(select(Budget).where(Budget.workflow_id == workflow.id))
        ).scalar_one()
        assert budget.cap_micro == 70
        await session.commit()
    from datetime import timedelta

    from policy import accounting

    following = (datetime.now(UTC).replace(day=1) + timedelta(days=32)).replace(day=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://admin",
        headers={"authorization": "Bearer test-admin"},
    ) as admin:
        for scope, identity in [
            ("client", governed.a.client.id),
            ("workflow", governed.a.workflow.id),
        ]:
            result = await admin.post(
                "/admin/budget-templates",
                json={
                    "scope": scope,
                    "scope_id": str(identity),
                    "cap_micro": 90,
                    "enabled": False,
                    "expected_revision": 1,
                    "reason": "Disable renewal",
                },
            )
            assert result.status_code == 201, result.text

    async def future(session):
        return following

    monkeypatch.setattr(accounting, "database_now", future)
    async with governed.factory() as session:
        assert await renew_current(session) == 0
        assert await reserve(session, governed.a.workflow.id, "disabled", 1, 1000) is None
        historical = (
            await session.execute(
                select(Budget).where(
                    Budget.workflow_id == governed.a.workflow.id, Budget.period == period
                )
            )
        ).scalar_one()
        assert historical.cap_micro == 70
        current = (
            await session.execute(
                select(ClientBudget).where(
                    ClientBudget.client_id == governed.a.client.id,
                    ClientBudget.period == f"{following:%Y-%m}",
                )
            )
        ).scalar_one()
        assert current.cap_micro is None


async def test_run_stop_does_not_stop_sibling_run_and_requires_room_before_clear(governed):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(policy={"models": ["*"], "per_run_micro": 10, "loop_breaker": {"max_steps": 1}})
        )
        await session.commit()
        first = await reserve(session, governed.a.workflow.id, "stopped", 1, 10, 1)
        await settle(session, first, 1)
        assert await reserve(session, governed.a.workflow.id, "stopped", 1, 10, 1) is None
        sibling = await reserve(session, governed.a.workflow.id, "sibling", 1, 10, 1)
        assert sibling is not None
        stopped = await session.get(Run, first.run_id)
        assert stopped.stopped
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://admin",
        headers={"authorization": "Bearer test-admin"},
    ) as admin:
        response = await admin.put(
            f"/admin/runs/{first.run_id}/stop",
            json={"active": False, "expected_revision": 1, "reason": "Resume"},
        )
        assert response.status_code == 409


async def test_utc_rollover_lazy_renewal_and_late_usage_keep_original_client_period(
    governed, monkeypatch
):
    from datetime import timedelta

    from policy import accounting
    from policy.accounting import apply
    from tests.test_accounting import pending_call

    held, measured = await pending_call(governed)
    original = f"{datetime.now(UTC):%Y-%m}"
    following = (datetime.now(UTC).replace(day=1) + timedelta(days=32)).replace(day=1)
    period = f"{following:%Y-%m}"
    async with governed.factory() as session:
        estimate = measured.model_copy(
            update={"kind": "estimate", "amount_micro": 80, "usage": None}
        )
        await apply(session, estimate)
        await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://admin",
        headers={"authorization": "Bearer test-admin"},
    ) as admin:
        for scope, identity in [
            ("client", governed.a.client.id),
            ("workflow", governed.a.workflow.id),
        ]:
            response = await admin.post(
                "/admin/budget-templates",
                json={
                    "scope": scope,
                    "scope_id": str(identity),
                    "cap_micro": 90,
                    "enabled": True,
                    "expected_revision": 0,
                    "reason": "Renew monthly",
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["effective_period"] == period

    async def future(session):
        return following

    monkeypatch.setattr(accounting, "database_now", future)
    async with governed.factory() as session:
        new = await reserve(session, governed.a.workflow.id, "new-period", 1, 1000)
        assert new is not None
        # A different evidence ID supplies terminal measured usage after rollover.
        import uuid

        await apply(session, measured.model_copy(update={"evidence_id": uuid.uuid4()}))
        await session.commit()
        buckets = {
            b.period: b
            for b in (
                await session.execute(
                    select(ClientBudget).where(ClientBudget.client_id == governed.a.client.id)
                )
            ).scalars()
        }
        assert (buckets[original].spent_micro, buckets[original].reserved_micro) == (15, 0)
        assert (
            buckets[period].spent_micro,
            buckets[period].reserved_micro,
            buckets[period].cap_micro,
        ) == (0, 1, 90)


async def test_lowering_cap_preserves_reservations_and_zero_cap_blocks_only_new_calls(governed):
    async with governed.factory() as session:
        held = await reserve(session, governed.a.workflow.id, "existing", 5, 100)
        assert held is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://admin",
        headers={"authorization": "Bearer test-admin"},
    ) as admin:
        body = {
            "period": f"{datetime.now(UTC):%Y-%m}",
            "cap_micro": 0,
            "expected_revision": 1,
            "reason": "Pause allowance",
        }
        response = await admin.put(f"/admin/clients/{governed.a.client.id}/budget", json=body)
        assert response.status_code == 200 and response.json()["reserved_micro"] == 5
    async with governed.factory() as session:
        assert await reserve(session, governed.a.workflow.id, "blocked", 1, 100) is None
        await settle(session, held, 3)
        bucket = (
            await session.execute(
                select(ClientBudget).where(ClientBudget.client_id == governed.a.client.id)
            )
        ).scalar_one()
        assert (bucket.spent_micro, bucket.reserved_micro) == (3, 0)
