"""Create a dedicated mock-only dashboard acceptance account, never a live provider key."""

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values


async def main() -> None:
    env = dict(os.environ)
    if not env.get("ADMIN_TOKEN"):
        env = {**dotenv_values(".env"), **env}
    async with httpx.AsyncClient(
        base_url=env.get("E2E_ADMIN_URL", "http://localhost:8001"),
        headers={"authorization": "Bearer " + str(env["ADMIN_TOKEN"])},
    ) as http:

        async def create(path: str, body: dict) -> dict:
            response = await http.post("/admin/" + path, json=body)
            response.raise_for_status()
            return response.json()

        suffix = secrets.token_hex(4)
        client = await create("clients", {"name": "Acceptance " + suffix})
        workflow = await create(
            "workflows",
            {
                "client_id": client["id"],
                "name": "Invoice assistant " + suffix,
                "risk_class": "R1",
                "policy": {"models": ["gpt-mock"], "per_run_micro": 10000},
            },
        )
        await create(
            "budgets",
            {
                "workflow_id": workflow["id"],
                "period": datetime.now(UTC).strftime("%Y-%m"),
                "cap_micro": 1000000,
            },
        )
        await create(
            "provider-credentials",
            {"client_id": client["id"], "provider": "openai", "key": "mock-only"},
        )
        # Repeated acceptance runs use the same deterministic mock meter.
        response = await http.post(
            "/admin/prices",
            json={
                "provider": "openai",
                "model": "gpt-mock",
                "effective_from": "2020-01-01",
                "input": 1000000,
                "output": 1000000,
                "reasoning": 1000000,
                "source_url": "https://example.test/mock-fixture",
            },
        )
        if response.status_code not in (201, 409):
            response.raise_for_status()
        password = secrets.token_urlsafe(24)
        email = "acceptance-" + suffix + "@example.test"
        await create(
            "users",
            {"email": email, "password": password, "role": "operator", "client_id": client["id"]},
        )
        path = Path(env.get("E2E_SEED_FILE", "/private/tmp/gas-e2e.json"))
        path.write_text(
            json.dumps(
                {
                    "email": email,
                    "password": password,
                    "client_id": client["id"],
                    "workflow_id": workflow["id"],
                    "name": workflow["name"],
                }
            )
        )
        path.chmod(0o600)
    print("Dedicated mock dashboard acceptance account created")


if __name__ == "__main__":
    asyncio.run(main())
