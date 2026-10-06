"""Native Anthropic Messages proxy route."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from audit.chain import AuditEventInput
from core.database import get_session
from core.models import Price, ProviderCredential
from core.security import decrypt_provider_key
from gateway.adapters.anthropic import MessageStreamTracker, message_usage
from gateway.auth import AuthContext, authenticate_anthropic
from gateway.errors import GatewayError
from policy.budget import (
    HeldReservation,
    cost_micro,
    current_price,
    estimate_micro,
    release,
    reserve,
    settle,
)
from policy.kill import check_kill

router = APIRouter()


def forwarded_headers(request: Request, provider_key: str) -> dict[str, str]:
    headers = {
        "x-api-key": provider_key,
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json",
        "accept-encoding": "identity",
    }
    for name in ("anthropic-beta",):
        if name in request.headers:
            headers[name] = request.headers[name]
    return headers


def response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        name: upstream.headers[name]
        for name in ("content-type", "request-id", "retry-after")
        if name in upstream.headers
    }


async def stream_messages(
    *,
    body: bytes,
    headers: dict[str, str],
    upstream_url: str,
    timeout: float,
    client_id: uuid.UUID,
    model: str,
    price: Price,
    held: HeldReservation,
    db_engine: AsyncEngine,
) -> Response:
    http = httpx.AsyncClient(timeout=timeout)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    request = http.build_request("POST", upstream_url, content=body, headers=headers)
    try:
        upstream = await http.send(request, stream=True)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        await http.aclose()
        async with factory() as session:
            await release(
                session, held, AuditEventInput(client_id, "provider_error", "anthropic", model)
            )
        code = 504 if isinstance(exc, httpx.TimeoutException) else 502
        raise GatewayError(code, "upstream_unavailable", "Anthropic is unavailable") from exc
    if upstream.status_code >= 400:
        error_body = await upstream.aread()
        async with factory() as session:
            await release(
                session, held, AuditEventInput(client_id, "provider_rejected", "anthropic", model)
            )
        await upstream.aclose()
        await http.aclose()
        return Response(
            error_body, status_code=upstream.status_code, headers=response_headers(upstream)
        )

    tracker = MessageStreamTracker()

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                tracker.feed(chunk)
                yield chunk
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
                            provider="anthropic",
                            model=model,
                            usage=usage.raw if usage is not None else None,
                            cost_micro=actual,
                            estimated=usage is None,
                        ),
                    )

            task = asyncio.create_task(finalize())
            await asyncio.shield(task)

    return StreamingResponse(
        chunks(), status_code=upstream.status_code, headers=response_headers(upstream)
    )


@router.post("/v1/messages")
async def messages(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_anthropic)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await check_kill(session, auth)
    body = await request.body()
    try:
        parsed: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GatewayError(400, "invalid_json", "Request body must be JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("model"), str):
        raise GatewayError(400, "invalid_request", "A model is required")
    model = parsed["model"]
    max_output = parsed.get("max_tokens")
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise GatewayError(400, "invalid_max_tokens", "max_tokens must be positive")
    price = await current_price(session, "anthropic", model)
    if price is None:
        raise GatewayError(400, "model_not_priced", "The model has no configured price")
    budget_policy = auth.policy.get("budget", {})
    per_run_cap = budget_policy.get("per_run_micro", 2**63 - 1)
    if isinstance(per_run_cap, bool) or not isinstance(per_run_cap, int) or per_run_cap <= 0:
        raise GatewayError(503, "invalid_policy", "Per-run cap is not configured correctly")
    now = datetime.now(UTC)
    run_key = request.headers.get("X-Run-Id") or f"window:{now:%Y%m%d%H}{now.minute // 10}"
    if len(run_key) > 200:
        raise GatewayError(400, "invalid_run_id", "X-Run-Id is too long")
    credential = (
        await session.execute(
            select(ProviderCredential).where(
                ProviderCredential.client_id == auth.client_id,
                ProviderCredential.provider == "anthropic",
                ProviderCredential.label == "default",
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise GatewayError(503, "provider_not_configured", "Anthropic credential is not configured")
    provider_key = decrypt_provider_key(
        credential.ciphertext,
        credential.nonce,
        credential.client_id,
        credential.provider,
        credential.label,
    )
    held = await reserve(
        session, auth.workflow_id, run_key, estimate_micro(body, max_output, price), per_run_cap
    )
    if held is None:
        raise GatewayError(429, "budget_exceeded", "Workflow budget exceeded")
    upstream_url = (
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        + "/v1/messages"
    )
    timeout = float(os.environ.get("UPSTREAM_TIMEOUT_S", "120"))
    headers = forwarded_headers(request, provider_key)
    if parsed.get("stream") is True:
        assert isinstance(session.bind, AsyncEngine)
        return await stream_messages(
            body=body,
            headers=headers,
            upstream_url=upstream_url,
            timeout=timeout,
            client_id=auth.client_id,
            model=model,
            price=price,
            held=held,
            db_engine=session.bind,
        )
    completed = False
    failure_type = "provider_error"
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            upstream = await http.post(upstream_url, content=body, headers=headers)
        if upstream.status_code < 400:
            usage = None
            try:
                response_body = upstream.json()
                if isinstance(response_body, dict):
                    usage = message_usage(response_body)
            except ValueError:
                pass
            actual = cost_micro(usage, price) if usage is not None else held.est_micro
            completed = await settle(
                session,
                held,
                actual,
                AuditEventInput(
                    client_id=auth.client_id,
                    type="provider_call",
                    provider="anthropic",
                    model=model,
                    usage=usage.raw if usage is not None else None,
                    cost_micro=actual,
                    estimated=usage is None,
                ),
            )
        else:
            failure_type = "provider_rejected"
    except httpx.TimeoutException as exc:
        raise GatewayError(504, "upstream_timeout", "Anthropic timed out") from exc
    except httpx.RequestError as exc:
        raise GatewayError(502, "upstream_unavailable", "Anthropic is unavailable") from exc
    finally:
        if not completed:
            await session.rollback()
            await release(
                session, held, AuditEventInput(auth.client_id, failure_type, "anthropic", model)
            )
    return Response(
        upstream.content, status_code=upstream.status_code, headers=response_headers(upstream)
    )
