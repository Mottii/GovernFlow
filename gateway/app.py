import json
import os
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from approvals.routes import router as approval_router
from core.database import get_session
from core.models import Price
from gateway.adapters.openai import ChatStreamTracker, chat_usage
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
from gateway.native import proxy_native
from gateway.openai_more import router as openai_more_router
from policy.kill import check_kill
from policy.traffic import allowed_models

app = FastAPI(title="Governed Automation Gateway")
app.add_exception_handler(GatewayError, openai_error_handler)
app.add_exception_handler(DBAPIError, database_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(HTTPException, http_error_handler)
app.include_router(anthropic_router)
app.include_router(openai_more_router)
app.include_router(gemini_router)
app.include_router(approval_router)


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
    max_output = parsed.get("max_completion_tokens", parsed.get("max_tokens"))
    if max_output is None:
        max_output = int(os.environ.get("DEFAULT_MAX_OUTPUT_TOKENS", "4096"))
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise GatewayError(400, "invalid_max_tokens", "Output token limit must be positive")

    def headers(key: str) -> dict[str, str]:
        return {"authorization": "Bearer " + key}

    return await proxy_native(
        request,
        auth,
        session,
        provider="openai",
        model=model,
        max_output=max_output,
        url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
        + "/v1/chat/completions",
        stream=parsed.get("stream") is True,
        headers_for_key=headers,
        usage_parser=chat_usage,
        tracker=ChatStreamTracker(suppress_injected_usage=False),
    )
