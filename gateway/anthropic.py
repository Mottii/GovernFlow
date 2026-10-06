"""Native Anthropic Messages; the shared relay parses a copy of streamed bytes."""

import json
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from gateway.adapters.anthropic import MessageStreamTracker, message_usage
from gateway.auth import AuthContext, authenticate_anthropic
from gateway.errors import GatewayError
from gateway.native import proxy_native

router = APIRouter()


@router.post("/v1/messages")
async def messages(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_anthropic)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    try:
        parsed: Any = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError) as exc:
        raise GatewayError(400, "invalid_json", "Request body must be JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("model"), str):
        raise GatewayError(400, "invalid_request", "A model is required")
    maximum = parsed.get("max_tokens")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise GatewayError(400, "invalid_max_tokens", "max_tokens must be positive")

    def headers(key: str) -> dict[str, str]:
        result = {
            "x-api-key": key,
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        }
        if "anthropic-beta" in request.headers:
            result["anthropic-beta"] = request.headers["anthropic-beta"]
        return result

    return await proxy_native(
        request,
        auth,
        session,
        provider="anthropic",
        model=parsed["model"],
        max_output=maximum,
        url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        + "/v1/messages",
        stream=parsed.get("stream") is True,
        headers_for_key=headers,
        usage_parser=message_usage,
        tracker=MessageStreamTracker(),
    )
