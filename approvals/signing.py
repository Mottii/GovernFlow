import base64
import hashlib
import hmac
import json
import time
from typing import Any

from gateway.errors import GatewayError


def secret(key_id: str | None = None) -> bytes:
    from core.keyrings import active_id, key

    try:
        return key("signing", key_id or active_id("signing"))
    except RuntimeError as error:
        raise GatewayError(
            503, "signing_unavailable", "Decision signing is not configured"
        ) from error


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign(payload: dict[str, Any]) -> str:
    from core.keyrings import active_id

    payload = {**payload, "kid": active_id("signing")}
    encoded = b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return encoded + "." + b64(hmac.digest(secret(), encoded.encode(), hashlib.sha256))


def verify(token: str, purpose: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".")
        preview = json.loads(unb64(encoded))
        identity = preview.get("kid", "0000")
        if not isinstance(identity, str):
            raise ValueError("key")
        from core.keyrings import key

        expected = hmac.digest(key("signing", identity), encoded.encode(), hashlib.sha256)
        if not hmac.compare_digest(unb64(signature), expected):
            raise ValueError("signature")
        payload = json.loads(unb64(encoded))
        if payload.get("purpose") != purpose or payload.get("exp", 0) <= time.time():
            raise ValueError("expiry")
        return dict(payload)
    except (ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise GatewayError(
            403, "invalid_decision_token", "Decision token is invalid or expired"
        ) from exc
