"""Bounded localhost mock benchmark; never sends real provider credentials."""

import argparse
import asyncio
import json
import os
import platform
import secrets
import statistics
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--rps", type=int, default=20)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--provider", default="http://localhost:8002")
    parser.add_argument("--admin", default="http://localhost:8001")
    args = parser.parse_args()
    for url in (args.gateway, args.provider, args.admin):
        if urlsplit(url).hostname not in (
            "localhost",
            "127.0.0.1",
            "gateway",
            "admin-api",
            "mock-provider",
        ):
            parser.error("Benchmark is restricted to explicit local mock endpoints")
    if not 20 <= args.requests <= 1000 or not 1 <= args.rps <= 100:
        parser.error("Use 20–1000 requests and 1–100 requests/second")
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        parser.error("Set ADMIN_TOKEN for the isolated mock installation")
    async with httpx.AsyncClient(
        base_url=args.admin, headers={"authorization": "Bearer " + token}, timeout=15
    ) as admin:

        async def create(path, body):
            response = await admin.post("/admin/" + path, json=body)
            response.raise_for_status()
            return response.json()

        suffix = secrets.token_hex(4)
        client = await create("clients", {"name": "Mock benchmark " + suffix})
        workflow = await create(
            "workflows",
            {
                "client_id": client["id"],
                "name": "Mock benchmark " + suffix,
                "risk_class": "R1",
                "policy": {
                    "models": ["gpt-mock"],
                    "rpm": 1000000,
                    "loop_breaker": {"rpm": 1000000, "max_steps": 1000000},
                    "per_run_micro": 1000000,
                },
            },
        )
        await create(
            "budgets",
            {
                "workflow_id": workflow["id"],
                "period": datetime.now(UTC).strftime("%Y-%m"),
                "cap_micro": 100000000,
            },
        )
        await create(
            "provider-credentials",
            {"client_id": client["id"], "provider": "openai", "key": "mock-only"},
        )
        price = await admin.post(
            "/admin/agency-prices",
            json={
                "provider": "openai",
                "model": "gpt-mock",
                "effective_from": "2020-01-01",
                "input": 1000000,
                "output": 1000000,
                "source_url": "https://example.test/mock-benchmark",
            },
        )
        if price.status_code not in (201, 409):
            price.raise_for_status()
        key = await create("keys", {"workflow_id": workflow["id"]})
    payload = {
        "model": "gpt-mock",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Synthetic benchmark input"}],
    }

    async def sample(base, key):
        async with httpx.AsyncClient(
            base_url=base, headers={"authorization": "Bearer " + key}, timeout=30
        ) as http:
            for _ in range(5):
                response = await http.post("/v1/chat/completions", json=payload)
                response.raise_for_status()
            start = time.monotonic()

            async def request(index):
                await asyncio.sleep(max(0, start + index / args.rps - time.monotonic()))
                before = time.monotonic()
                response = await http.post(
                    "/v1/chat/completions",
                    json=payload,
                    headers={"x-run-id": f"benchmark:{suffix}:{index}"},
                )
                response.raise_for_status()
                return (time.monotonic() - before) * 1000

            return await asyncio.gather(*(request(i) for i in range(args.requests)))

    direct = await sample(args.provider, "mock-only")
    governed = await sample(args.gateway, key["key"])
    added = [max(0, g - p) for g, p in zip(governed, direct, strict=True)]
    result = {
        "at": datetime.now(UTC).isoformat(),
        "client_platform": platform.platform(),
        "requests": args.requests,
        "rps": args.rps,
        "provider_p95_ms": round(percentile(direct, 0.95), 3),
        "gateway_p95_ms": round(percentile(governed, 0.95), 3),
        "added_p50_ms": round(statistics.median(added), 3),
        "added_p95_ms": round(percentile(added, 0.95), 3),
        "added_p95_target_ms": 100,
        "passed": percentile(added, 0.95) < 100,
    }
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
