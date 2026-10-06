import json
import os
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.models import ProviderCredential
from core.security import decrypt_provider_key
from gateway.adapters.openai import chat_usage
from gateway.auth import AuthContext, authenticate_openai
from gateway.errors import GatewayError, openai_error_handler

app = FastAPI(title="Governed Automation Gateway")
app.add_exception_handler(GatewayError, openai_error_handler)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate_openai)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    body = await request.body()
    try:
        parsed: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GatewayError(400, "invalid_json", "Request body must be JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("model"), str):
        raise GatewayError(400, "invalid_request", "A model is required")
    if parsed.get("stream"):
        raise GatewayError(501, "stream_not_available", "Streaming is not implemented yet")

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
    except httpx.TimeoutException as exc:
        raise GatewayError(504, "upstream_timeout", "Upstream provider timed out") from exc
    except httpx.RequestError as exc:
        raise GatewayError(502, "upstream_unavailable", "Upstream provider is unavailable") from exc

    if upstream.status_code < 400:
        try:
            response_body = upstream.json()
            if isinstance(response_body, dict):
                chat_usage(response_body)
        except ValueError:
            pass
    response_headers: dict[str, str] = {}
    for header in ("content-type", "x-request-id", "retry-after"):
        if header in upstream.headers:
            response_headers[header] = upstream.headers[header]
    return Response(
        content=upstream.content, status_code=upstream.status_code, headers=response_headers
    )
