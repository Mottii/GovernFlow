"""Deployment-owned key material; only identifiers enter persistent application state."""

import base64
import json
import os
import re
from typing import Literal

Kind = Literal["encryption", "signing"]


def ring(kind: Kind) -> dict[str, bytes]:
    name = "ENCRYPTION_KEYS" if kind == "encryption" else "SIGNING_KEYS"
    legacy = "MASTER_ENCRYPTION_KEY" if kind == "encryption" else "DECISION_SIGNING_KEY"
    try:
        encoded = json.loads(os.environ.get(name) or "{}")
        if not isinstance(encoded, dict) or len(encoded) > 32:
            raise ValueError()
        if os.environ.get(legacy):
            encoded.setdefault("0000", os.environ[legacy])
        result = {}
        for identity, value in encoded.items():
            if not re.fullmatch(r"[0-9a-f]{4}", identity):
                raise ValueError()
            key = base64.b64decode(value, validate=True)
            if len(key) != 32:
                raise ValueError()
            result[identity] = key
        return result
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError(
            name + " must map four-digit lowercase hex IDs to base64 32-byte keys"
        ) from None


def active_id(kind: Kind) -> str:
    name = "ACTIVE_ENCRYPTION_KEY_ID" if kind == "encryption" else "ACTIVE_SIGNING_KEY_ID"
    identity = os.environ.get(name, "0000")
    if identity not in ring(kind):
        raise RuntimeError(name + " is unavailable in its key ring")
    return identity


def key(kind: Kind, identity: str | None) -> bytes:
    value = ring(kind).get(identity or "0000")
    if value is None:
        raise RuntimeError("Required " + kind + " key is unavailable")
    return value
