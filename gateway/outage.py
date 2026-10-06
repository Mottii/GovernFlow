"""Bounded replica cache for administrator-enabled R0/R1 outage forwarding."""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from core.models import Price
from core.security import virtual_key_hash
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from policy.budget import estimate_micro
from policy.replay import enqueue, replace


@dataclass
class Snapshot:
    auth: AuthContext
    price: Price
    headers: dict[str, str]
    url: str
    expires: float
    remaining: int
    calls: int = 0


_cache: dict[tuple[bytes, str], Snapshot] = {}
_lock = asyncio.Lock()


def request_key(request: Request) -> str:
    return (
        request.headers.get("authorization", "").removeprefix("Bearer ")
        or request.headers.get("x-api-key")
        or request.headers.get("x-goog-api-key")
        or request.query_params.get("key", "")
    )


def remember(
    request: Request,
    auth: AuthContext,
    price: Price,
    headers: dict[str, str],
    url: str,
    remaining: int,
) -> None:
    outage = auth.policy.get("outage", {})
    if (
        auth.risk_class in ("R0", "R1")
        and isinstance(outage, dict)
        and outage.get("mode") == "open"
    ):
        # Memory only; signing/encryption/provider keys never enter the disk queue.
        _cache[(virtual_key_hash(request_key(request)), request.url.path)] = Snapshot(
            auth, price, headers, url, time.monotonic() + 60, remaining
        )


async def forward(request: Request) -> Response | None:
    if getattr(request.state, "upstream_started", False):
        return None  # Never repeat a provider call after a settlement failure.
    snapshot = _cache.get((virtual_key_hash(request_key(request)), request.url.path))
    if snapshot is None or snapshot.expires < time.monotonic():
        return None
    body = await request.body()
    try:
        parsed = json.loads(body)
        model = parsed.get("model", request.path_params.get("model_action", "").split(":")[0])
        if model != snapshot.price.model:
            return None
        maximum = parsed.get(
            "max_completion_tokens",
            parsed.get(
                "max_tokens",
                parsed.get(
                    "max_output_tokens",
                    parsed.get("generationConfig", {}).get("maxOutputTokens", 4096),
                ),
            ),
        )
        if request.url.path == "/v1/embeddings":
            maximum = 0
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            return None
    except (ValueError, AttributeError):
        return None
    cost = estimate_micro(body, maximum, snapshot.price)
    async with _lock:
        if (
            snapshot.calls >= min(snapshot.auth.policy.get("rpm", 60), 100)
            or snapshot.remaining < cost
        ):
            raise GatewayError(429, "outage_limit", "Replica outage allowance exhausted")
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "client_id": str(snapshot.auth.client_id),
            "workflow_id": str(snapshot.auth.workflow_id),
            "provider": snapshot.price.provider,
            "model": model,
            "cost_micro": cost,
            "period": now.strftime("%Y-%m"),
            "ready": False,
            "queued_at": now.timestamp(),
            "run_key": request.headers.get("x-run-id") or f"outage:{now:%Y%m%d%H%M}",
        }
        identity = await enqueue(payload)  # FULL SQLite sync must finish before forwarding.
        snapshot.remaining -= cost
        snapshot.calls += 1
    stream = parsed.get("stream") is True or request.url.path.endswith(":streamGenerateContent")
    http = httpx.AsyncClient(timeout=float(os.environ.get("UPSTREAM_TIMEOUT_S", "120")))
    try:
        upstream = await http.send(
            http.build_request("POST", snapshot.url, content=body, headers=snapshot.headers),
            stream=stream,
        )
    except httpx.RequestError as exc:
        await replace(identity, {**payload, "cost_micro": 0, "ready": True})
        await http.aclose()
        raise GatewayError(502, "upstream_unavailable", "Provider is unavailable") from exc
    headers = {
        name: upstream.headers[name]
        for name in ("content-type", "x-request-id", "request-id", "retry-after")
        if name in upstream.headers
    }
    headers["x-gateway-estimated"] = "true"
    headers["x-gateway-outage"] = "fail-open"
    if upstream.status_code >= 400:
        await replace(identity, {**payload, "cost_micro": 0, "ready": True})
        result = await upstream.aread()
        await upstream.aclose()
        await http.aclose()
        return Response(result, status_code=upstream.status_code, headers=headers)
    await replace(identity, {**payload, "ready": True})
    if stream:

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await http.aclose()

        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers)
    result = upstream.content
    await http.aclose()
    return Response(result, status_code=upstream.status_code, headers=headers)
