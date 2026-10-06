import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from gateway.errors import GatewayError


def secret() -> bytes:
    try:
        value = base64.b64decode(os.environ.get("DECISION_SIGNING_KEY", ""), validate=True)
    except ValueError as exc:
        raise GatewayError(
            503, "signing_unavailable", "Decision signing is not configured"
        ) from exc
    if len(value) != 32:
        raise GatewayError(503, "signing_unavailable", "Decision signing is not configured")
    return value


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign(payload: dict[str, Any]) -> str:
    encoded = b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return encoded + "." + b64(hmac.digest(secret(), encoded.encode(), hashlib.sha256))


def verify(token: str, purpose: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".")
        expected = hmac.digest(secret(), encoded.encode(), hashlib.sha256)
        if not hmac.compare_digest(unb64(signature), expected):
            raise ValueError("signature")
        payload = json.loads(unb64(encoded))
        if payload.get("purpose") != purpose or payload.get("exp", 0) <= time.time():
            raise ValueError("expiry")
        return dict(payload)
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(
            403, "invalid_decision_token", "Decision token is invalid or expired"
        ) from exc
