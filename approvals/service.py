import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.models import Approval, Workflow
from core.security import decrypt_provider_key, encrypt_provider_key
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from policy.kill import check_kill


class ActionCheck(BaseModel):
    action_name: str = Field(min_length=1, max_length=100)
    action: dict[str, Any]
    summary: str = Field(min_length=1, max_length=2000)


class Decision(BaseModel):
    decision: Literal["approve", "deny"]
    approver: str = Field(min_length=1, max_length=200)


def action_hash(name: str, action: dict[str, Any]) -> bytes:
    try:
        canonical = json.dumps(
            {"name": name, "action": action},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise GatewayError(400, "invalid_action", "Action must be valid JSON") from exc
    return hashlib.sha256(canonical.encode()).digest()


def view(record: Approval) -> dict[str, Any]:
    return {
        "approval_id": str(record.id),
        "status": record.status,
        "decision": "allow"
        if record.status == "approved"
        else "deny"
        if record.status in ("denied", "expired")
        else "pending",
        "expires_at": record.expires_at.isoformat(),
    }


def content(record: Approval, client_id: uuid.UUID) -> dict[str, Any]:
    if record.content_ciphertext is None or record.content_nonce is None:
        return {}
    return cast(
        dict[str, Any],
        json.loads(
            decrypt_provider_key(
                record.content_ciphertext, record.content_nonce, client_id, "action", str(record.id)
            )
        ),
    )


async def check(session: AsyncSession, auth: AuthContext, body: ActionCheck) -> dict[str, Any]:
    await check_kill(session, auth)
    actions = auth.policy.get("actions", {})
    if not isinstance(actions, dict):
        raise GatewayError(503, "invalid_policy", "Action policy is invalid")
    rule = actions.get(body.action_name)
    if rule is None and auth.policy.get("strict_actions", True):
        await append_event(
            session,
            AuditEventInput(
                auth.client_id,
                "action_denied",
                content_ref="action-hash:" + action_hash(body.action_name, body.action).hex(),
            ),
        )
        await session.commit()
        return {"decision": "deny", "status": "denied", "reason": "unknown_action"}
    if rule is None:
        rule = {}
    if not isinstance(rule, dict) or rule.get("risk_class", auth.risk_class) not in (
        "R0",
        "R1",
        "R2",
        "R3",
    ):
        raise GatewayError(503, "invalid_policy", "Action risk is invalid")
    risk = max(auth.risk_class, rule.get("risk_class", auth.risk_class))
    timeout = rule.get("timeout_seconds", 3600)
    outcome = rule.get("on_timeout", "deny")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 604800:
        raise GatewayError(503, "invalid_policy", "Action timeout is invalid")
    if outcome not in ("allow", "deny") or (outcome == "allow" and risk >= "R2"):
        raise GatewayError(503, "invalid_policy", "High-risk actions must deny on timeout")
    now = datetime.now(UTC)
    record = Approval(
        id=uuid.uuid4(),
        workflow_id=auth.workflow_id,
        action_name=body.action_name,
        action_hash=action_hash(body.action_name, body.action),
        action_summary=body.summary,
        status="approved" if risk < "R2" else "pending",
        expires_at=now + timedelta(seconds=timeout),
        timeout_outcome=outcome,
        approver="policy" if risk < "R2" else None,
        decided_at=now if risk < "R2" else None,
    )
    record.content_ciphertext, record.content_nonce = encrypt_provider_key(
        json.dumps(body.action, allow_nan=False), auth.client_id, "action", str(record.id)
    )
    session.add(record)
    await append_event(
        session,
        AuditEventInput(
            auth.client_id, "action_proposed", content_ref="approval:" + str(record.id)
        ),
    )
    if record.status == "approved":
        await append_event(
            session,
            AuditEventInput(
                auth.client_id, "action_approved", content_ref="approval:" + str(record.id)
            ),
        )
    await session.commit()
    return view(record)


async def scoped(session: AsyncSession, auth: AuthContext, approval_id: uuid.UUID) -> Approval:
    record = (
        await session.execute(
            select(Approval).where(
                Approval.id == approval_id, Approval.workflow_id == auth.workflow_id
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    return record


async def decide(session: AsyncSession, approval_id: uuid.UUID, body: Decision) -> dict[str, Any]:
    record = (
        await session.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
    ).scalar_one_or_none()
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    if record.status != "pending" or record.expires_at <= datetime.now(UTC):
        raise GatewayError(409, "approval_not_pending", "Approval is no longer pending")
    record.status = "approved" if body.decision == "approve" else "denied"
    record.approver = body.approver
    record.decided_at = datetime.now(UTC)
    client_id = (
        await session.execute(select(Workflow.client_id).where(Workflow.id == record.workflow_id))
    ).scalar_one()
    await append_event(
        session,
        AuditEventInput(
            client_id, "action_" + record.status, content_ref="approval:" + str(record.id)
        ),
    )
    await session.commit()
    return view(record)
