import json
import os
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.exceptions import HTTPException

from audit.chain import AuditEventInput
from core.database import get_session
from core.models import Price, ProviderCredential
from core.security import decrypt_provider_key
from gateway.adapters.openai import chat_usage
from gateway.anthropic import router as anthropic_router
from gateway.auth import AuthContext, authenticate_openai
from gateway.errors import (
    GatewayError,
    database_error_handler,
    http_error_handler,
    openai_error_handler,
    validation_error_handler,
)
from gateway.gemini import router as gemini_router
from gateway.openai_more import router as openai_more_router
from gateway.streaming import stream_openai_chat
from policy.budget import cost_micro, current_price, estimate_micro, release, reserve, settle
from policy.kill import check_kill
from policy.traffic import allowed_models, check_traffic

app = FastAPI(title="Governed Automation Gateway")
app.add_exception_handler(GatewayError, openai_error_handler)
app.add_exception_handler(DBAPIError, database_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(HTTPException, http_error_handler)
app.include_router(anthropic_router)
app.include_router(openai_more_router)
app.include_router(gemini_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(
    auth: Annotated[AuthContext, Depends(authenticate_openai)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await check_kill(session, auth)
    allowed = allowed_models(auth)
    names = (
        await session.execute(
            select(Price.model)
            .where(Price.provider == "openai", Price.effective_from <= datetime.now(UTC).date())
            .distinct()
            .order_by(Price.model)
        )
    ).scalars()
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "openai"}
            for name in names
            if any(fnmatchcase(name, pattern) for pattern in allowed)
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_openai)],
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
    if (
        parsed.get("stream")
        and parsed.get("stream_options") is not None
        and not isinstance(parsed["stream_options"], dict)
    ):
        raise GatewayError(400, "invalid_stream_options", "stream_options must be an object")
    model = parsed["model"]
    await check_traffic(session, auth, model)
    price = await current_price(session, "openai", model)
    if price is None:
        raise GatewayError(400, "model_not_priced", "The model has no configured price")
    max_output = parsed.get("max_completion_tokens", parsed.get("max_tokens"))
    if max_output is None:
        max_output = int(os.environ.get("DEFAULT_MAX_OUTPUT_TOKENS", "4096"))
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise GatewayError(400, "invalid_max_tokens", "Output token limit must be positive")
    budget_policy = auth.policy.get("budget", {})
    per_run_cap = budget_policy.get("per_run_micro", 2**63 - 1)
    if isinstance(per_run_cap, bool) or not isinstance(per_run_cap, int) or per_run_cap <= 0:
        raise GatewayError(503, "invalid_policy", "Per-run cap is not configured correctly")
    now = datetime.now(UTC)
    run_key = request.headers.get("X-Run-Id")
    if not run_key:
        run_key = f"window:{now:%Y%m%d%H}{now.minute // 10}"
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
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    timeout = float(os.environ.get("UPSTREAM_TIMEOUT_S", "120"))
    held = await reserve(
        session,
        auth.workflow_id,
        run_key,
        estimate_micro(body, max_output, price),
        per_run_cap,
    )
    if held is None:
        raise GatewayError(429, "budget_exceeded", "Workflow budget exceeded")
    if parsed.get("stream"):
        assert isinstance(session.bind, AsyncEngine)
        return await stream_openai_chat(
            body=body,
            parsed=parsed,
            client_id=auth.client_id,
            model=model,
            provider_key=provider_key,
            upstream_url=f"{base_url}/v1/chat/completions",
            timeout=timeout,
            price=price,
            held=held,
            db_engine=session.bind,
        )
    completed = False
    failure_type = "provider_error"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(
                f"{base_url}/v1/chat/completions",
                content=body,
                headers={
                    "Authorization": f"Bearer {provider_key}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
        if upstream.status_code < 400:
            usage = None
            try:
                response_body = upstream.json()
                if isinstance(response_body, dict):
                    usage = chat_usage(response_body)
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
            if not completed:
                raise GatewayError(500, "settlement_failed", "Could not settle reservation")
        else:
            failure_type = "provider_rejected"
    except httpx.TimeoutException as exc:
        raise GatewayError(504, "upstream_timeout", "Upstream provider timed out") from exc
    except httpx.RequestError as exc:
        raise GatewayError(502, "upstream_unavailable", "Upstream provider is unavailable") from exc
    finally:
        if not completed:
            await session.rollback()
            await release(
                session,
                held,
                AuditEventInput(
                    client_id=auth.client_id,
                    type=failure_type,
                    provider="openai",
                    model=model,
                    cost_micro=0,
                ),
            )
    response_headers: dict[str, str] = {}
    for header in ("content-type", "x-request-id", "retry-after"):
        if header in upstream.headers:
            response_headers[header] = upstream.headers[header]
    return Response(
        content=upstream.content, status_code=upstream.status_code, headers=response_headers
    )
