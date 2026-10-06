"""Native OpenAI Responses and embeddings routes."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from audit.chain import AuditEventInput
from core.database import get_session
from core.models import Price, ProviderCredential
from core.security import decrypt_provider_key
from core.usage import Usage
from gateway.adapters.openai_more import ResponsesStreamTracker, embeddings_usage, responses_usage
from gateway.auth import AuthContext, authenticate_openai
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
from policy.loop import loop_policy
from policy.traffic import check_traffic

router = APIRouter()
Endpoint = Literal["responses", "embeddings"]


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        name: upstream.headers[name]
        for name in ("content-type", "x-request-id", "retry-after")
        if name in upstream.headers
    }


async def _stream_responses(
    body: bytes,
    upstream_url: str,
    headers: dict[str, str],
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
                session, held, AuditEventInput(client_id, "provider_error", "openai", model)
            )
        code = 504 if isinstance(exc, httpx.TimeoutException) else 502
        raise GatewayError(code, "upstream_unavailable", "OpenAI is unavailable") from exc
    if upstream.status_code >= 400:
        error_body = await upstream.aread()
        async with factory() as session:
            await release(
                session, held, AuditEventInput(client_id, "provider_rejected", "openai", model)
            )
        await upstream.aclose()
        await http.aclose()
        return Response(
            error_body, status_code=upstream.status_code, headers=_response_headers(upstream)
        )
    tracker = ResponsesStreamTracker()

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
                            provider="openai",
                            model=model,
                            usage=usage.raw if usage is not None else None,
                            cost_micro=actual,
                            estimated=usage is None,
                        ),
                    )

            task = asyncio.create_task(finalize())
            await asyncio.shield(task)

    return StreamingResponse(
        chunks(), status_code=upstream.status_code, headers=_response_headers(upstream)
    )


async def _proxy(
    endpoint: Endpoint,
    request: Request,
    auth: AuthContext,
    session: AsyncSession,
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
    await check_traffic(session, auth, model)
    max_output = parsed.get("max_output_tokens", 4096) if endpoint == "responses" else 0
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 0:
        raise GatewayError(400, "invalid_max_output_tokens", "Output token limit is invalid")
    price = await current_price(session, "openai", model)
    if price is None:
        raise GatewayError(400, "model_not_priced", "The model has no configured price")
    per_run_cap, max_steps, _ = loop_policy(auth.policy)
    now = datetime.now(UTC)
    run_key = request.headers.get("X-Run-Id") or f"window:{now:%Y%m%d%H}{now.minute // 10}"
    if len(run_key) > 200:
        raise GatewayError(400, "invalid_run_id", "X-Run-Id is too long")
    credential = (
        await session.execute(
            select(ProviderCredential).where(
                ProviderCredential.client_id == auth.client_id,
                ProviderCredential.provider == "openai",
                ProviderCredential.label == "default",
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise GatewayError(503, "provider_not_configured", "OpenAI credential is not configured")
    provider_key = decrypt_provider_key(
        credential.ciphertext,
        credential.nonce,
        credential.client_id,
        credential.provider,
        credential.label,
    )
    held = await reserve(
        session,
        auth.workflow_id,
        run_key,
        estimate_micro(body, max_output, price),
        per_run_cap,
        max_steps,
    )
    if held is None:
        raise GatewayError(429, "budget_exceeded", "Workflow budget exceeded")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    upstream_url = f"{base_url}/v1/{endpoint}"
    timeout = float(os.environ.get("UPSTREAM_TIMEOUT_S", "120"))
    headers = {
        "Authorization": f"Bearer {provider_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }
    if "openai-beta" in request.headers:
        headers["OpenAI-Beta"] = request.headers["openai-beta"]
    if endpoint == "responses" and parsed.get("stream") is True:
        assert isinstance(session.bind, AsyncEngine)
        return await _stream_responses(
            body, upstream_url, headers, timeout, auth.client_id, model, price, held, session.bind
        )
    completed = False
    failure_type = "provider_error"
    usage_parser: Callable[[dict[str, Any]], Usage | None] = (
        responses_usage if endpoint == "responses" else embeddings_usage
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            upstream = await http.post(upstream_url, content=body, headers=headers)
        if upstream.status_code < 400:
            usage = None
            try:
                response_body = upstream.json()
                if isinstance(response_body, dict):
                    usage = usage_parser(response_body)
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
                    provider="openai",
                    model=model,
                    usage=usage.raw if usage is not None else None,
                    cost_micro=actual,
                    estimated=usage is None,
                ),
            )
        else:
            failure_type = "provider_rejected"
    except httpx.TimeoutException as exc:
        raise GatewayError(504, "upstream_timeout", "OpenAI timed out") from exc
    except httpx.RequestError as exc:
        raise GatewayError(502, "upstream_unavailable", "OpenAI is unavailable") from exc
    finally:
        if not completed:
            await session.rollback()
            await release(
                session, held, AuditEventInput(auth.client_id, failure_type, "openai", model)
            )
    return Response(
        upstream.content, status_code=upstream.status_code, headers=_response_headers(upstream)
    )


@router.post("/v1/responses")
async def responses(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_openai)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    return await _proxy("responses", request, auth, session)


@router.post("/v1/embeddings")
async def embeddings(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_openai)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    return await _proxy("embeddings", request, auth, session)
