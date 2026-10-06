import os
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.models import Alert, ReconcileSnapshot


async def detect(session: AsyncSession, client_id: uuid.UUID, provider: str, day: date) -> None:
    key = f"reconcile:{client_id}:{provider}:{day}"
    # Serialize detection and resolution, including multiple worker replicas.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    rows = (
        (
            await session.execute(
                select(ReconcileSnapshot).where(
                    ReconcileSnapshot.client_id == client_id,
                    ReconcileSnapshot.provider == provider,
                    ReconcileSnapshot.day == day,
                )
            )
        )
        .scalars()
        .all()
    )
    cost_tolerance = max(0, int(os.environ.get("DRIFT_COST_TOLERANCE_MICRO", "1")))
    token_tolerance = max(0, int(os.environ.get("DRIFT_TOKEN_TOLERANCE", "0")))
    differences: dict[str, Any] = {}
    positive = False
    estimated = 0
    for row in rows:
        if row.kind == "cost":
            delta = row.observed["cost_micro"] - row.gateway["cost_micro"]
            estimated = row.gateway.get("estimated_calls", 0)
            if abs(delta) > cost_tolerance:
                differences["cost_micro"] = delta
                positive |= delta > 0
        else:
            values = {}
            for field in ("input_tokens", "output_tokens"):
                delta = row.observed[field] - row.gateway[field]
                if abs(delta) > token_tolerance:
                    values[field] = delta
                    positive |= delta > 0
            if values:
                differences[row.dimension] = values
    existing = (
        await session.execute(select(Alert).where(Alert.dedup_key == key))
    ).scalar_one_or_none()
    if not differences:
        if existing and not existing.details.get("resolved"):
            existing.details = {**existing.details, "resolved": True}
            await append_event(
                session,
                AuditEventInput(client_id, "drift_resolved", provider, usage={"day": str(day)}),
            )
        return
    # Estimated streams have unknown token totals and require review.
    kind = "possible_bypass" if positive and not estimated else "reconciliation_drift"
    details = {
        "provider": provider,
        "day": str(day),
        "differences": differences,
        "estimated_calls": estimated,
        "resolved": False,
        "status": "possible bypass" if kind == "possible_bypass" else "drift requires review",
        "caveat": "Delays, UTC boundaries, estimates or billing categories may explain drift",
    }
    if existing:
        reopened = existing.details.get("resolved") or existing.kind != kind
        existing.kind, existing.details = kind, details
        if not reopened:
            return
    else:
        session.add(Alert(client_id=client_id, kind=kind, dedup_key=key, details=details))
    await append_event(
        session, AuditEventInput(client_id, "drift_detected", provider, usage=details)
    )
