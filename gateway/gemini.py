import os
import re
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from gateway.adapters.gemini import ContentStreamTracker, content_usage
from gateway.auth import AuthContext, resolve_virtual_key
from gateway.errors import GatewayError
from gateway.native import proxy_native

router = APIRouter()


async def authenticate_gemini(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> AuthContext:
    key = request.headers.get("x-goog-api-key") or request.query_params.get("key")
    if not key:
        raise GatewayError(401, "invalid_api_key", "A valid virtual key is required")
    return await resolve_virtual_key(session, key)


@router.post("/{version}/models/{model_action}")
async def generate_content(
    version: str,
    model_action: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_gemini)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    match = re.fullmatch(r"([A-Za-z0-9._-]+):(generateContent|streamGenerateContent)", model_action)
    if version not in ("v1", "v1beta") or not match:
        raise GatewayError(404, "not_found", "Unknown content generation route")
    try:
        parsed = await request.json()
    except ValueError as exc:
        raise GatewayError(400, "invalid_json", "Request body must be JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("generationConfig", {}), dict):
        raise GatewayError(400, "invalid_request", "Invalid generation request")
    max_output = parsed.get("generationConfig", {}).get("maxOutputTokens", 4096)
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise GatewayError(400, "invalid_max_tokens", "Output token limit must be positive")
    model, action = match.groups()
    query = request.query_params.multi_items()
    query_string = str(httpx.QueryParams([(k, v) for k, v in query if k != "key"]))
    url = (
        os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
        + f"/{version}/models/{model_action}"
    )
    if query_string:
        url += "?" + query_string
    return await proxy_native(
        request,
        auth,
        session,
        provider="gemini",
        model=model,
        max_output=max_output,
        url=url,
        stream=action == "streamGenerateContent",
        headers_for_key=lambda key: {
            "x-goog-api-key": key,
            "x-goog-api-client": "governed-automation/0.1.0",
        },
        usage_parser=content_usage,
        tracker=ContentStreamTracker(request.query_params.get("alt") == "sse"),
    )
