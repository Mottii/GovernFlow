"""Bounded replica cache for administrator-enabled R0/R1 outage forwarding."""

import asyncio
import json
import os
import time
import uuid
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
from policy.billing import standard_meter
from policy.budget import estimate_micro
from policy.journal import CallSeed, JournalRecord, persist


@dataclass
class Snapshot:
    auth: AuthContext
    price: Price
    headers: dict[str, str]
    url: str
    expires: float
    remaining: int
    calls: int = 0
    seed: CallSeed | None = None
    run_key: str = ""


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
    seed: CallSeed,
    run_key: str,
) -> None:
    if len(_cache) >= 10000:
        _cache.pop(next(iter(_cache)))
    outage = auth.policy.get("outage", {})
    if (
        auth.risk_class in ("R0", "R1")
        and isinstance(outage, dict)
        and outage.get("mode") == "open"
    ):
        # Memory only; signing/encryption/provider keys never enter the disk queue.
        _cache[(virtual_key_hash(request_key(request)), request.url.path)] = Snapshot(
            auth, price, headers, url, time.monotonic() + 60, remaining, seed=seed, run_key=run_key
        )


async def forward(request: Request) -> Response | None:
    if (
        os.environ.get("DEPLOYMENT_MODE", "self_hosted") != "self_hosted"
        or os.environ.get("ALLOW_FAIL_OPEN", "false").lower() != "true"
        or os.environ.get("SELF_HOSTED_SINGLE_NODE", "false").lower() != "true"
        or os.environ.get("RECOVERY_BACKEND", "sqlite") != "sqlite"
        or getattr(request.state, "upstream_started", False)
    ):
        return None
    snapshot = _cache.get((virtual_key_hash(request_key(request)), request.url.path))
    if snapshot is None or snapshot.expires < time.monotonic() or snapshot.seed is None:
        return None
    now = datetime.now(UTC)
    run_key = request.headers.get("x-run-id") or f"window:{now:%Y%m%d%H}{now.minute // 10}"
    if run_key != snapshot.run_key or snapshot.seed.period != now.strftime("%Y-%m"):
        return None
    from gateway.limits import output_limit
    from gateway.runtime import runtime

    body, maximum = output_limit(
        await request.body(), snapshot.price.provider, request.url.path, snapshot.auth
    )
    parsed = json.loads(body)
    model = parsed.get("model", request.path_params.get("model_action", "").split(":")[0])
    if model != snapshot.price.model:
        return None
    standard_meter(body, snapshot.price.provider, snapshot.price)
    cost = estimate_micro(body, maximum, snapshot.price)
    service = runtime(request)
    service.workflow(snapshot.auth.workflow_id)
    request.state.admitted_workflow = snapshot.auth.workflow_id
    identity = uuid.uuid4()
    seed = snapshot.seed.model_copy(update={"estimate_micro": cost})
    intent = JournalRecord(
        evidence_id=uuid.uuid5(identity, "outage-intent"),
        agency_id=snapshot.auth.agency_id,
        client_id=snapshot.auth.client_id,
        call_id=identity,
        kind="estimate",
        amount_micro=cost,
        reason="outage_dispatch_intent",
        seed=seed,
    )
    async with _lock:
        if (
            snapshot.calls >= min(snapshot.auth.policy.get("rpm", 60), 100)
            or snapshot.remaining < cost
        ):
            raise GatewayError(429, "outage_limit", "Replica outage allowance exhausted")
        await persist(intent.model_dump(mode="json"), store=service.journal)
        snapshot.remaining -= cost
        snapshot.calls += 1
    request.state.call_id = str(identity)
    request.state.upstream_started = True
    stream = parsed.get("stream") is True or request.url.path.endswith(":streamGenerateContent")
    from gateway.adapters.anthropic import MessageStreamTracker, message_usage
    from gateway.adapters.gemini import ContentStreamTracker, content_usage
    from gateway.adapters.openai import ChatStreamTracker, chat_usage
    from gateway.adapters.openai_more import (
        ResponsesStreamTracker,
        embeddings_usage,
        responses_usage,
    )
    from policy.budget import cost_micro

    tracker: Any
    parser: Any
    if snapshot.price.provider == "anthropic":
        tracker, parser = MessageStreamTracker(), message_usage
    elif snapshot.price.provider == "gemini":
        tracker, parser = (
            ContentStreamTracker(request.query_params.get("alt") == "sse"),
            content_usage,
        )
    elif request.url.path == "/v1/responses":
        tracker, parser = ResponsesStreamTracker(), responses_usage
    else:
        tracker, parser = (
            ChatStreamTracker(suppress_injected_usage=False),
            embeddings_usage if request.url.path == "/v1/embeddings" else chat_usage,
        )
    expires = asyncio.get_running_loop().time() + int(
        os.environ.get("CALL_DEADLINE_SECONDS", "300")
    )

    async def finish(usage: Any, *, void: bool = False) -> None:
        kind = "void" if void else "measured" if usage else "estimate"
        partial = getattr(tracker, "partial_usage", None)
        amount = (
            0
            if void
            else cost_micro(usage, snapshot.price)
            if usage
            else max(cost, cost_micro(partial, snapshot.price) if partial else 0)
        )
        evidence = intent.model_copy(
            update={
                "evidence_id": uuid.uuid5(identity, "outage-final:" + kind),
                "kind": kind,
                "amount_micro": amount,
                "reason": "not_transmitted"
                if void
                else "terminal_usage"
                if usage
                else "outage_usage_missing",
                "usage": {
                    name: getattr(usage, name)
                    for name in (
                        "input",
                        "output",
                        "cache_read",
                        "cache_write",
                        "cache_write_hour",
                        "reasoning",
                    )
                }
                if usage
                else None,
            }
        )
        task = asyncio.create_task(service.preserve(evidence.model_dump(mode="json")))
        service.track(task)
        await asyncio.shield(task)

    try:
        async with asyncio.timeout_at(expires):
            upstream = await service.http.send(
                service.http.build_request(
                    "POST", snapshot.url, content=body, headers=snapshot.headers
                ),
                stream=True,
            )
    except BaseException as error:
        await finish(
            None,
            void=isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)),
        )
        if isinstance(error, (httpx.RequestError, TimeoutError)):
            raise GatewayError(502, "upstream_unavailable", "Provider is unavailable") from error
        raise
    headers = {
        name: upstream.headers[name]
        for name in ("content-type", "x-request-id", "request-id", "retry-after")
        if name in upstream.headers
    }
    headers.update(
        {
            "x-gateway-call-id": str(identity),
            "x-gateway-estimated": "true",
            "x-gateway-outage": "fail-open",
        }
    )
    if stream and upstream.status_code < 400:

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async with asyncio.timeout_at(expires):
                    async for part in upstream.aiter_raw():
                        tracker.feed(part)
                        yield part
            finally:

                async def cleanup() -> None:
                    try:
                        await upstream.aclose()
                    finally:
                        await finish(tracker.usage)

                task = asyncio.create_task(cleanup())
                service.track(task)
                await asyncio.shield(task)

        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers)
    try:
        data = bytearray()
        async with asyncio.timeout_at(expires):
            async for part in upstream.aiter_bytes():
                data.extend(part)
                if len(data) > int(os.environ.get("MAX_RESPONSE_BYTES", str(16 * 1024 * 1024))):
                    raise GatewayError(
                        502, "response_too_large", "Provider response exceeds buffer limit"
                    )
        try:
            result = json.loads(data)
            usage = (
                parser(result) if isinstance(result, dict) and upstream.status_code < 400 else None
            )
            rejected = (
                upstream.status_code in (400, 401, 403, 404, 413, 422, 429)
                and isinstance(result, dict)
                and isinstance(result.get("error"), (dict, str))
            )
        except ValueError:
            usage, rejected = None, False
        await finish(usage, void=rejected)
        headers["x-gateway-estimated"] = "false" if usage else "true"
        return Response(bytes(data), status_code=upstream.status_code, headers=headers)
    except BaseException:
        await finish(None)
        raise
    finally:
        await upstream.aclose()
