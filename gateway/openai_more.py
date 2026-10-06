"""Native OpenAI Responses and embeddings."""

import json
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from gateway.adapters.openai_more import ResponsesStreamTracker, embeddings_usage, responses_usage
from gateway.auth import AuthContext, authenticate_openai
from gateway.errors import GatewayError
from gateway.native import proxy_native

router = APIRouter()


async def _proxy(
    endpoint: Literal["responses", "embeddings"],
    request: Request,
    auth: AuthContext,
    session: AsyncSession,
) -> Response:
    try:
        parsed: Any = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError) as exc:
        raise GatewayError(400, "invalid_json", "Request body must be JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("model"), str):
        raise GatewayError(400, "invalid_request", "A model is required")
    maximum = parsed.get("max_output_tokens", 4096) if endpoint == "responses" else 0
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise GatewayError(400, "invalid_max_tokens", "Output token limit is invalid")

    def headers(key: str) -> dict[str, str]:
        result = {"authorization": "Bearer " + key}
        if "openai-beta" in request.headers:
            result["openai-beta"] = request.headers["openai-beta"]
        return result

    return await proxy_native(
        request,
        auth,
        session,
        provider="openai",
        model=parsed["model"],
        max_output=maximum,
        url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
        + "/v1/"
        + endpoint,
        stream=endpoint == "responses" and parsed.get("stream") is True,
        headers_for_key=headers,
        usage_parser=responses_usage if endpoint == "responses" else embeddings_usage,
        tracker=ResponsesStreamTracker(),
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
