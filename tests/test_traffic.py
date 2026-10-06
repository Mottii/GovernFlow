import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

from core.models import RateBucket, Workflow
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from policy.traffic import check_traffic
from tests.test_native_errors import ROUTES, headers, native


@pytest.mark.asyncio
async def test_model_denial_on_all_routes(governed):
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(policy={"models": []})
        )
        await session.commit()
    for path, provider, model, extra in ROUTES:
        native(
            await governed.http.post(
                path, json={"model": model, **extra}, headers=headers(provider, governed.a.key)
            ),
            provider,
            403,
        )


@pytest.mark.asyncio
async def test_shared_rpm_race_and_retry_after(governed):
    auth = AuthContext(
        governed.a.client.id, governed.a.workflow.id, "R1", {"models": ["*"], "rpm": 7}, "test"
    )

    async def attempt():
        async with governed.factory() as session:
            try:
                await check_traffic(session, auth, "gpt-mock")
                return 200
            except GatewayError as exc:
                assert exc.retry_after and 1 <= exc.retry_after <= 60
                return exc.status_code

    statuses = await asyncio.gather(*(attempt() for _ in range(50)))
    assert statuses.count(200) == 7 and statuses.count(429) == 43
    async with governed.factory() as session:
        await session.execute(delete(RateBucket))
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(policy={"models": ["*"], "rpm": 1})
        )
        session.add(
            RateBucket(
                workflow_id=governed.a.workflow.id,
                minute=datetime.now(UTC).replace(second=0, microsecond=0),
                count=1,
            )
        )
        await session.commit()
    for path, provider, model, extra in ROUTES:
        response = await governed.http.post(
            path, json={"model": model, **extra}, headers=headers(provider, governed.a.key)
        )
        native(response, provider, 429)
        assert 1 <= int(response.headers["retry-after"]) <= 60
