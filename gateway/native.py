"""Shared reservation and byte-preserving relay for native provider routes."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from audit.chain import AuditEventInput
from core.models import ProviderCredential
from core.security import decrypt_provider_key
from core.usage import Usage
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from policy.budget import cost_micro, current_price, estimate_micro, release, reserve, settle
from policy.kill import check_kill


class Tracker(Protocol):
    def feed(self, chunk: bytes) -> None: ...
    @property
    def usage(self) -> Usage | None: ...


async def proxy_native(
    request: Request,
    auth: AuthContext,
    session: AsyncSession,
    *,
    provider: str,
    model: str,
    max_output: int,
    url: str,
    stream: bool,
    headers_for_key: Callable[[str], dict[str, str]],
    usage_parser: Callable[[dict[str, Any]], Usage | None],
    tracker: Tracker,
) -> Response:
    await check_kill(session, auth)
    body = await request.body()
    price = await current_price(session, provider, model)
    if price is None:
        raise GatewayError(400, "model_not_priced", "The model has no configured price")
    budget_policy = auth.policy.get("budget", {})
    cap = budget_policy.get("per_run_micro", 2**63 - 1)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise GatewayError(503, "invalid_policy", "Per-run cap is invalid")
    now = datetime.now(UTC)
    run_key = request.headers.get("X-Run-Id") or f"window:{now:%Y%m%d%H}{now.minute // 10}"
    if len(run_key) > 200:
        raise GatewayError(400, "invalid_run_id", "X-Run-Id is too long")
    credential = (
        await session.execute(
            select(ProviderCredential).where(
                ProviderCredential.client_id == auth.client_id,
                ProviderCredential.provider == provider,
                ProviderCredential.label == "default",
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise GatewayError(503, "provider_not_configured", "Provider credential is not configured")
    key = decrypt_provider_key(
        credential.ciphertext,
        credential.nonce,
        credential.client_id,
        credential.provider,
        credential.label,
    )
    held = await reserve(
        session, auth.workflow_id, run_key, estimate_micro(body, max_output, price), cap
    )
    if held is None:
        raise GatewayError(429, "budget_exceeded", "Workflow budget exceeded")
    assert isinstance(session.bind, AsyncEngine)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    http = httpx.AsyncClient(timeout=float(os.environ.get("UPSTREAM_TIMEOUT_S", "120")))
    headers = {
        "content-type": "application/json",
        "accept-encoding": "identity",
        **headers_for_key(key),
    }
    upstream_request = http.build_request("POST", url, content=body, headers=headers)
    try:
        upstream = await http.send(upstream_request, stream=stream)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        await http.aclose()
        await release(
            session, held, AuditEventInput(auth.client_id, "provider_error", provider, model)
        )
        raise GatewayError(
            504 if isinstance(exc, httpx.TimeoutException) else 502,
            "upstream_unavailable",
            "Provider is unavailable",
        ) from exc
    response_headers = {
        name: upstream.headers[name]
        for name in ("content-type", "x-request-id", "request-id", "retry-after")
        if name in upstream.headers
    }
    if upstream.status_code >= 400:
        result = await upstream.aread()
        await release(
            session, held, AuditEventInput(auth.client_id, "provider_rejected", provider, model)
        )
        await upstream.aclose()
        await http.aclose()
        return Response(result, status_code=upstream.status_code, headers=response_headers)

    async def finalize(usage: Usage | None) -> None:
        actual = cost_micro(usage, price) if usage is not None else held.est_micro
        async with factory() as final_session:
            await settle(
                final_session,
                held,
                actual,
                AuditEventInput(
                    client_id=auth.client_id,
                    type="provider_call",
                    provider=provider,
                    model=model,
                    usage=usage.raw if usage is not None else None,
                    cost_micro=actual,
                    estimated=usage is None,
                ),
            )

    if stream:

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    tracker.feed(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                await http.aclose()
                await asyncio.shield(asyncio.create_task(finalize(tracker.usage)))

        return StreamingResponse(
            chunks(), status_code=upstream.status_code, headers=response_headers
        )
    usage = None
    try:
        result_json = upstream.json()
        if isinstance(result_json, dict):
            usage = usage_parser(result_json)
    except ValueError:
        pass
    await finalize(usage)
    result = upstream.content
    await http.aclose()
    return Response(result, status_code=upstream.status_code, headers=response_headers)
