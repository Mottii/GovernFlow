"""Shared reservation and byte-preserving relay for native provider routes."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.database import set_agency_context
from core.models import (
    Budget,
    CredentialVersion,
    ProviderAccount,
    ProviderCall,
    ProviderCredential,
    Run,
)
from core.security import decrypt_provider_key
from core.usage import Usage
from gateway.adapters.openai import ChatStreamTracker
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from gateway.outage import remember
from policy.billing import standard_meter
from policy.budget import cost_micro, current_price, estimate_micro, reserve
from policy.kill import check_kill
from policy.loop import loop_policy
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
    from gateway.limits import output_limit

    body, max_output = output_limit(await request.body(), provider, request.url.path, auth)
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
            select(ProviderCredential)
            .where(
                ProviderCredential.client_id == auth.client_id,
                ProviderCredential.provider == provider,
                ProviderCredential.label == (account.credential_label if account else "default"),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if credential is None:
        raise GatewayError(503, "provider_not_configured", "Provider credential is not configured")
    selected = (
        await session.get(CredentialVersion, credential.active_version_id)
        if credential.active_version_id
        else None
    )
    if selected is None:
        from core.database import agency_rls_enabled

        has_history = (
            await session.execute(
                select(CredentialVersion.id)
                .where(CredentialVersion.credential_id == credential.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if agency_rls_enabled() or has_history is not None:
            raise GatewayError(
                503, "credential_retired", "No active credential version is configured"
            )
        # Metadata-only legacy fixtures use the original slot without migration backfill.
    if selected is not None and (
        selected.state != "active" or selected.credential_id != credential.id
    ):
        raise GatewayError(503, "credential_retired", "No active credential version is configured")
    if (
        required
        and selected is not None
        and account is not None
        and account.validated_credential_version_id != selected.id
    ):
        raise GatewayError(
            503, "provider_account_unmapped", "Validate the current credential version"
        )
    try:
        key = decrypt_provider_key(
            selected.ciphertext if selected else credential.ciphertext,
            selected.nonce if selected else credential.nonce,
            credential.client_id,
            credential.provider,
            credential.label,
            key_id=selected.encryption_key_id if selected else credential.encryption_key_id,
        )
    except (InvalidTag, RuntimeError, UnicodeDecodeError) as exc:
        raise GatewayError(
            503, "credential_unavailable", "Provider credential cannot be decrypted"
        ) from exc
    from gateway.runtime import positive_setting, runtime
    from policy.accounting import (
        Evidence,
        EvidenceConflict,
        apply,
        mark_dispatched,
        price_snapshot,
        renew,
    )

    service = runtime(request)
    service.workflow(auth.workflow_id)
    request.state.admitted_workflow = auth.workflow_id
    from policy.revisions import lock as policy_lock

    current_policy = await policy_lock(session, auth.workflow_id)
    if current_policy.policy_version != auth.policy_version:
        raise GatewayError(
            409, "policy_changed", "Policy changed before admission; retry the request"
        )
    held = await reserve(
        session,
        auth.workflow_id,
        run_key,
        estimate_micro(body, max_output, price),
        cap,
        max_steps,
        provider=provider,
        model=model,
        pricing=price_snapshot(price),
    )
    if held is None:
        raise GatewayError(429, "budget_exceeded", "Workflow budget exceeded")
    call = await session.get(ProviderCall, held.id)
    assert call is not None
    call.credential_version_id = selected.id if selected else None
    call.policy_version = auth.policy_version
    assert isinstance(session.bind, AsyncEngine)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    http = service.http
    headers = {
        "content-type": "application/json",
        "accept-encoding": "identity",
        **headers_for_key(key),
    }
    if provider == "openai" and account is not None:
        headers["OpenAI-Project"] = account.external_id
    budget = (await session.execute(select(Budget).where(Budget.id == held.budget_id))).scalar_one()
    from core.models import ClientBudget

    shared_bucket = await session.get(ClientBudget, call.client_budget_id)
    assert shared_bucket is not None
    run = (await session.execute(select(Run).where(Run.id == held.run_id))).scalar_one()
    from policy.journal import CallSeed

    remember(
        request,
        auth,
        price,
        headers,
        url,
        min(
            budget.cap_micro - budget.spent_micro - budget.reserved_micro,
            run.cap_micro - run.cost_micro - run.reserved_micro,
            shared_bucket.cap_micro - shared_bucket.spent_micro - shared_bucket.reserved_micro
            if shared_bucket.cap_micro is not None
            else 2**63 - 1,
        ),
        seed=CallSeed(
            client_budget_id=call.client_budget_id,
            policy_version=call.policy_version,
            credential_version_id=call.credential_version_id,
            budget_id=budget.id,
            run_id=run.id,
            workflow_id=auth.workflow_id,
            period=budget.period,
            provider=provider,
            model=model,
            estimate_micro=held.est_micro,
            pricing=price_snapshot(price),
        ),
        run_key=run_key,
    )
    await session.commit()
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
    owner = uuid.uuid4()
    await mark_dispatched(session, held.id, owner)
    request.state.call_id = str(held.id)
    request.state.upstream_started = True
    provider_id: str | None = None
    finished = False
    active_task = [asyncio.current_task()]
    expires = asyncio.get_running_loop().time() + positive_setting("CALL_DEADLINE_SECONDS", 300)

    async def heartbeat() -> None:
        last_success = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(positive_setting("CALL_RENEW_SECONDS", 15))
            try:
                async with factory() as lease_session:
                    await set_agency_context(lease_session, auth.agency_id)
                    if not await renew(lease_session, held.id, owner):
                        if active_task[0]:
                            active_task[0].cancel()
                        return
                last_success = asyncio.get_running_loop().time()
            except (DBAPIError, OSError, TimeoutError):
                if asyncio.get_running_loop().time() - last_success >= positive_setting(
                    "CALL_LEASE_SECONDS", 60
                ):
                    if active_task[0]:
                        active_task[0].cancel()
                    return

    lease_task = asyncio.create_task(heartbeat())
    service.track(lease_task)

    async def finalize(
        usage: Usage | None, *, void: bool = False, reason: str = "missing_terminal_usage"
    ) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
        partial: Usage | None = getattr(tracker, "partial_usage", None)
        amount = (
            0
            if void
            else cost_micro(usage, price)
            if usage
            else max(held.est_micro, cost_micro(partial, price) if partial else 0)
        )
        kind: Literal["void", "measured", "estimate"] = (
            "void" if void else "measured" if usage else "estimate"
        )
        normalized = (
            {
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
            else None
        )
        evidence = Evidence(
            evidence_id=uuid.uuid5(held.id, "terminal:" + kind),
            agency_id=auth.agency_id,
            client_id=auth.client_id,
            call_id=held.id,
            kind=kind,
            amount_micro=amount,
            reason="terminal_usage" if usage else reason,
            usage=normalized,
            provider_request_id=provider_id,
        )
        try:
            async with asyncio.timeout(10), factory() as final_session:
                await set_agency_context(final_session, auth.agency_id)
                await apply(final_session, evidence)
                await final_session.commit()
        except (DBAPIError, OSError, TimeoutError, EvidenceConflict):
            await service.preserve(evidence.model_dump(mode="json"))

    async def durable_finish(
        usage: Usage | None, *, void: bool = False, reason: str = "missing_terminal_usage"
    ) -> None:
        task = asyncio.create_task(finalize(usage, void=void, reason=reason))
        service.track(task)
        await asyncio.shield(task)

    try:
        async with asyncio.timeout_at(expires):
            upstream = await http.send(upstream_request, stream=True)
    except BaseException as exc:
        # Connection establishment/pool failures prove that no HTTP request was sent.
        known_not_sent = isinstance(
            exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        )
        await durable_finish(
            None,
            void=known_not_sent,
            reason="not_transmitted" if known_not_sent else "dispatch_interrupted",
        )
        if isinstance(exc, (httpx.RequestError, TimeoutError)):
            raise GatewayError(
                504 if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else 502,
                "upstream_unavailable",
                "Provider is unavailable",
            ) from exc
        raise
    provider_id = (
        upstream.headers.get("x-request-id") or upstream.headers.get("request-id") or ""
    )[:200] or None
    response_headers = {
        name: upstream.headers[name]
        for name in ("content-type", "x-request-id", "request-id", "retry-after")
        if name in upstream.headers
    }
    response_headers["x-gateway-call-id"] = str(held.id)

    async def read_response() -> bytes:
        result = bytearray()
        async with asyncio.timeout_at(expires):
            async for part in upstream.aiter_bytes():
                result.extend(part)
                if len(result) > positive_setting("MAX_RESPONSE_BYTES", 16 * 1024 * 1024):
                    raise GatewayError(
                        502, "response_too_large", "Provider response exceeds buffer limit"
                    )
        return bytes(result)

    if upstream.status_code >= 400:
        try:
            result = await read_response()
            try:
                native_error = isinstance(json.loads(result).get("error"), (dict, str))
            except (ValueError, AttributeError):
                native_error = False
            nonbillable = native_error and upstream.status_code in (
                400,
                401,
                403,
                404,
                413,
                422,
                429,
            )
            await durable_finish(
                None,
                void=nonbillable,
                reason="provider_rejected" if nonbillable else "provider_error_uncertain",
            )
            return Response(result, status_code=upstream.status_code, headers=response_headers)
        finally:
            await upstream.aclose()
            if not finished:
                await durable_finish(None, reason="error_response_interrupted")

    if stream:

        async def chunks() -> AsyncIterator[bytes]:
            active_task[0] = asyncio.current_task()
            try:
                async with asyncio.timeout_at(expires):
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

                async def cleanup() -> None:
                    try:
                        await upstream.aclose()
                    finally:
                        await finalize(tracker.usage, reason="stream_incomplete")

                task = asyncio.create_task(cleanup())
                service.track(task)
                await asyncio.shield(task)

        return StreamingResponse(
            chunks(), status_code=upstream.status_code, headers=response_headers
        )
    usage = None
    try:
        result = await read_response()
        try:
            parsed_result = json.loads(result)
            if isinstance(parsed_result, dict):
                usage = usage_parser(parsed_result)
        except ValueError:
            pass
        await durable_finish(usage)
        return Response(result, status_code=upstream.status_code, headers=response_headers)
    finally:
        await upstream.aclose()
        if not finished:
            await durable_finish(None, reason="response_interrupted")
