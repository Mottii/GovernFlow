import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.expiry import expire_record
from approvals.signing import sign
from audit.chain import AuditEventInput, append_event
from core.models import Approval, Notification, Workflow
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
    result: dict[str, Any] = {
        "approval_id": str(record.id),
        "status": record.status,
        "decision": "allow"
        if record.status == "approved"
        else "deny"
        if record.status in ("denied", "expired")
        else "pending",
        "expires_at": record.expires_at.isoformat(),
    }
    if record.status == "expired" and record.timeout_outcome == "allow":
        result["decision"] = "allow"
    if (
        record.status == "approved"
        or record.status == "expired"
        and record.timeout_outcome == "allow"
    ) and record.decided_at:
        expiry = record.decided_at + timedelta(minutes=15)
        if record.status == "approved":
            expiry = min(record.expires_at, expiry)
        if record.consumed_at is not None or expiry <= datetime.now(UTC):
            result["decision"] = "deny"
            result["reason"] = "consumed" if record.consumed_at else "token_expired"
        else:
            result["token"] = sign(
                {
                    "purpose": "execute",
                    "approval_id": str(record.id),
                    "workflow_id": str(record.workflow_id),
                    "action_hash": record.action_hash.hex(),
                    "exp": int(expiry.timestamp()),
                }
            )
    return result


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
    require_approval = rule.get("require_approval", False)
    if not isinstance(require_approval, bool):
        raise GatewayError(503, "invalid_policy", "require_approval must be boolean")
    automatic = risk < "R2" and not require_approval
    now = datetime.now(UTC)
    record = Approval(
        id=uuid.uuid4(),
        workflow_id=auth.workflow_id,
        action_name=body.action_name,
        action_hash=action_hash(body.action_name, body.action),
        action_summary=body.summary,
        status="approved" if automatic else "pending",
        expires_at=now + timedelta(seconds=timeout),
        timeout_outcome=outcome,
        approver="policy" if automatic else None,
        decided_at=now if automatic else None,
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
    if record.status == "pending":
        channels = auth.policy.get("approval_channels", ["telegram"])
        if not isinstance(channels, list) or any(
            channel not in ("telegram", "email", "webhook") for channel in channels
        ):
            raise GatewayError(503, "invalid_policy", "Approval notification channels are invalid")
        await session.flush()
        for channel in set(channels):
            session.add(Notification(approval_id=record.id, channel=channel))
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
    if await expire_record(session, record):
        await session.commit()
    if record.status != "pending":
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
