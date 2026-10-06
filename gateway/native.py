"""Shared reservation and byte-preserving relay for native provider routes."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from audit.chain import AuditEventInput
from core.models import Budget, ProviderAccount, ProviderCredential, Run
from core.security import decrypt_provider_key
from core.usage import Usage
from gateway.adapters.openai import ChatStreamTracker
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from gateway.outage import remember
from policy.billing import standard_meter
from policy.budget import cost_micro, current_price, estimate_micro, release, reserve, settle
from policy.kill import check_kill
from policy.loop import loop_policy
from policy.replay import enqueue
from policy.traffic import check_traffic


class Tracker(Protocol):
    def feed(self, chunk: bytes) -> object: ...
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
    await check_traffic(session, auth, model)
    body = await request.body()
    price = await current_price(session, provider, model)
    if price is None:
        raise GatewayError(400, "model_not_priced", "The model has no configured price")
    standard_meter(body, provider, price)
    cap, max_steps, _ = loop_policy(auth.policy)
    now = datetime.now(UTC)
    run_key = request.headers.get("X-Run-Id") or f"window:{now:%Y%m%d%H}{now.minute // 10}"
    if len(run_key) > 200:
        raise GatewayError(400, "invalid_run_id", "X-Run-Id is too long")
    account = (
        await session.get(ProviderAccount, (auth.client_id, provider))
        if provider != "gemini"
        else None
    )
    live = httpx.URL(url).host in ("api.openai.com", "api.anthropic.com")
    required = live or os.environ.get("REQUIRE_PROVIDER_MAPPING", "false").lower() == "true"
    if (
        provider != "gemini"
        and required
        and (account is None or (live and account.validated_at is None))
    ):
        raise GatewayError(
            503, "provider_account_unmapped", "A validated dedicated provider account is required"
        )
    credential = (
        await session.execute(
            select(ProviderCredential).where(
                ProviderCredential.client_id == auth.client_id,
                ProviderCredential.provider == provider,
                ProviderCredential.label == (account.credential_label if account else "default"),
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise GatewayError(503, "provider_not_configured", "Provider credential is not configured")
    try:
        key = decrypt_provider_key(
            credential.ciphertext,
            credential.nonce,
            credential.client_id,
            credential.provider,
            credential.label,
        )
    except (InvalidTag, RuntimeError, UnicodeDecodeError) as exc:
        raise GatewayError(
            503, "credential_unavailable", "Provider credential cannot be decrypted"
        ) from exc
    held = await reserve(
        session, auth.workflow_id, run_key, estimate_micro(body, max_output, price), cap, max_steps
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
    if provider == "openai" and account is not None:
        headers["OpenAI-Project"] = account.external_id
    budget = (await session.execute(select(Budget).where(Budget.id == held.budget_id))).scalar_one()
    run = (await session.execute(select(Run).where(Run.id == held.run_id))).scalar_one()
    remember(
        request,
        auth,
        price,
        headers,
        url,
        min(
            budget.cap_micro - budget.spent_micro - budget.reserved_micro,
            run.cap_micro - run.cost_micro - run.reserved_micro,
        ),
    )
    upstream_body = body
    if stream and isinstance(tracker, ChatStreamTracker):
        parsed = json.loads(body)
        options = parsed.get("stream_options") or {}
        if options.get("include_usage") is not True:
            tracker.suppress_injected_usage = True
            upstream_body = json.dumps(
                {**parsed, "stream_options": {**options, "include_usage": True}},
                separators=(",", ":"),
            ).encode()
    upstream_request = http.build_request("POST", url, content=upstream_body, headers=headers)
    request.state.upstream_started = True
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
        try:
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
        except DBAPIError:
            await enqueue(
                {
                    "client_id": str(auth.client_id),
                    "workflow_id": str(auth.workflow_id),
                    "provider": provider,
                    "model": model,
                    "cost_micro": actual,
                    "held": {
                        "id": str(held.id),
                        "budget_id": str(held.budget_id),
                        "run_id": str(held.run_id),
                        "est_micro": held.est_micro,
                    },
                }
            )

    if stream:

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    if isinstance(tracker, ChatStreamTracker):
                        for frame in tracker.feed(chunk):
                            yield frame
                    else:
                        tracker.feed(chunk)
                        yield chunk
                if isinstance(tracker, ChatStreamTracker):
                    remaining = tracker.flush()
                    if remaining:
                        yield remaining
            finally:

                async def finish() -> None:
                    await upstream.aclose()
                    await http.aclose()
                    await finalize(tracker.usage)

                await asyncio.shield(asyncio.create_task(finish()))

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
