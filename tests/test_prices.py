from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from admin_api.app import app as admin_app
from core.models import Price
from core.pricing import catalog, seed
from core.usage import Usage
from gateway.adapters.anthropic import message_usage
from policy.budget import cost_micro


@pytest.mark.asyncio
async def test_verified_catalog_seed_admin_and_exact_cost(governed):
    async with governed.factory() as session:
        assert await seed(session) == 6
        assert await seed(session) == 6
        for item in catalog()["prices"]:
            price = (
                await session.execute(
                    select(Price).where(
                        Price.provider == item["provider"], Price.model == item["model"]
                    )
                )
            ).scalar_one()
            assert price.source_url.startswith("https://") and price.verified_at.tzinfo
        price = (
            await session.execute(select(Price).where(Price.model == "gpt-6-sol"))
        ).scalar_one()
        assert cost_micro(Usage(input=300000, output=1000), price) == 1215000
        haiku = (
            await session.execute(select(Price).where(Price.model == "claude-haiku-4-5"))
        ).scalar_one()
        usage = message_usage(
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 1000,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 600,
                        "ephemeral_1h_input_tokens": 400,
                    },
                }
            }
        )
        assert usage.cache_write == 600 and usage.cache_write_hour == 400
        assert cost_micro(usage, haiku) == 1700
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as admin:
        body = {
            "provider": "openai",
            "model": "custom",
            "effective_from": datetime.now(UTC).date().isoformat(),
            "input": 1,
            "output": 1,
            "source_url": "https://provider.example/prices",
        }
        assert (await admin.post("/admin/prices", json=body)).status_code == 401
        assert (
            await admin.post(
                "/admin/prices", json=body, headers={"authorization": "Bearer test-admin"}
            )
        ).status_code == 201
        assert (
            await admin.post(
                "/admin/prices",
                json={**body, "input": -1},
                headers={"authorization": "Bearer test-admin"},
            )
        ).status_code == 422
    response = await governed.http.post(
        "/v1/responses",
        json={"model": "gpt-6-sol", "input": "hello", "tools": [{"type": "web_search"}]},
        headers={"authorization": "Bearer " + governed.a.key},
    )
    assert (
        response.status_code == 400
        and response.headers["x-gateway-error"] == "billing_option_not_priced"
    )
