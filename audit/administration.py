"""Transactional append-only agency administration history, without secret payloads."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import AdministrativeEvent

# Deliberately allowlist metadata; callers cannot accidentally dump request bodies.
ALLOWED = {
    "reason",
    "version",
    "previous_version",
    "expected_version",
    "policy_hash",
    "risk_class",
    "cap_micro",
    "previous_cap_micro",
    "period",
    "enabled",
    "scope",
    "effective_period",
    "role",
    "active",
    "all_clients",
    "client_ids",
    "group_ids",
    "workflow_ids",
    "provider",
    "label",
    "state",
    "key_id",
    "count",
    "changed_fields",
}


async def record(
    session: AsyncSession,
    agency_id: uuid.UUID,
    actor: str,
    resource: str,
    identity: object,
    action: str,
    **details: Any,
) -> AdministrativeEvent:
    if set(details) - ALLOWED:
        raise ValueError("Administrative audit metadata is not allowlisted")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": "administration:" + str(agency_id)},
    )
    previous = (
        await session.execute(
            select(AdministrativeEvent.hash)
            .where(AdministrativeEvent.agency_id == agency_id)
            .order_by(AdministrativeEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or bytes(32)
    at = datetime.now(UTC)
    payload = {
        "agency_id": str(agency_id),
        "actor": actor,
        "resource": resource,
        "resource_id": str(identity),
        "action": action,
        "details": details,
        "created_at": at.isoformat(),
    }
    item = AdministrativeEvent(
        agency_id=agency_id,
        actor=actor,
        resource=resource,
        resource_id=str(identity),
        action=action,
        details=details,
        created_at=at,
        previous_hash=previous,
        hash=hashlib.sha256(
            previous + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).digest(),
    )
    session.add(item)
    await session.flush()
    return item
