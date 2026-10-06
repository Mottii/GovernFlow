import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from audit.chain import AuditEventInput
from core.models import Price
from gateway.adapters.openai import ChatStreamTracker
from gateway.errors import GatewayError
from policy.budget import HeldReservation, cost_micro, release, settle


async def stream_openai_chat(
    *,
    body: bytes,
    parsed: dict[str, Any],
    client_id: uuid.UUID,
    model: str,
    provider_key: str,
    upstream_url: str,
    timeout: float,
    price: Price,
    held: HeldReservation,
    db_engine: AsyncEngine,
) -> Response:
    options = parsed.get("stream_options") or {}
    if not isinstance(options, dict):
        raise GatewayError(400, "invalid_stream_options", "stream_options must be an object")
    client_requested_usage = isinstance(options, dict) and options.get("include_usage") is True
    if client_requested_usage:
        upstream_body = body
    else:
        upstream_body = json.dumps(
            {**parsed, "stream_options": {**options, "include_usage": True}},
            separators=(",", ":"),
        ).encode()
    tracker = ChatStreamTracker(suppress_injected_usage=not client_requested_usage)
    http = httpx.AsyncClient(timeout=timeout)
    request = http.build_request(
        "POST",
        upstream_url,
        content=upstream_body,
        headers={
            "Authorization": f"Bearer {provider_key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        upstream = await http.send(request, stream=True)
    except httpx.TimeoutException as exc:
        await http.aclose()
        async with factory() as session:
            await release(
                session, held, AuditEventInput(client_id, "provider_error", "openai", model)
            )
        raise GatewayError(504, "upstream_timeout", "Upstream provider timed out") from exc
    except httpx.RequestError as exc:
        await http.aclose()
        async with factory() as session:
            await release(
                session, held, AuditEventInput(client_id, "provider_error", "openai", model)
            )
        raise GatewayError(502, "upstream_unavailable", "Upstream provider is unavailable") from exc

    if upstream.status_code >= 400:
        error_body = await upstream.aread()
        headers = {
            name: upstream.headers[name]
            for name in ("content-type", "retry-after")
            if name in upstream.headers
        }
        async with factory() as session:
            await release(
                session, held, AuditEventInput(client_id, "provider_rejected", "openai", model)
            )
        await upstream.aclose()
        await http.aclose()
        return Response(error_body, status_code=upstream.status_code, headers=headers)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                for frame in tracker.feed(chunk):
                    yield frame
            remaining = tracker.flush()
            if remaining:
                yield remaining
        finally:
            await upstream.aclose()
            await http.aclose()
            usage = tracker.usage
            actual = cost_micro(usage, price) if usage is not None else held.est_micro

            async def finalize() -> None:
                async with factory() as session:
                    await settle(
                        session,
                        held,
                        actual,
                        AuditEventInput(
                            client_id=client_id,
                            type="provider_call",
                            provider="openai",
                            model=model,
                            usage=usage.raw if usage is not None else None,
                            cost_micro=actual,
                            estimated=usage is None,
                        ),
                    )

            task = asyncio.create_task(finalize())
            await asyncio.shield(task)

    response_headers = {
        name: upstream.headers[name]
        for name in ("content-type", "x-request-id", "retry-after")
        if name in upstream.headers
    }
    return StreamingResponse(chunks(), status_code=upstream.status_code, headers=response_headers)
