import hmac
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.service import action_hash
from approvals.signing import verify
from audit.chain import AuditEventInput, append_event
from core.models import Approval
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from policy.kill import check_kill


class ActionConsume(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    action_name: str = Field(min_length=1, max_length=100)
    action: dict[str, Any]


async def consume(session: AsyncSession, auth: AuthContext, body: ActionConsume) -> dict[str, Any]:
    await check_kill(session, auth)
    payload = verify(body.token, "execute")
    try:
        identity = uuid.UUID(payload["approval_id"])
        supplied_hash = bytes.fromhex(payload["action_hash"])
        if payload["workflow_id"] != str(auth.workflow_id):
            raise ValueError("workflow")
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayError(
            403, "invalid_decision_token", "Execution token binding is invalid"
        ) from exc
    from policy.revisions import lock

    workflow = await lock(session, auth.workflow_id)
    record = (
        await session.execute(
            select(Approval)
            .where(Approval.id == identity, Approval.workflow_id == auth.workflow_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        record is None
        or not hmac.compare_digest(record.action_hash, supplied_hash)
        or not hmac.compare_digest(record.action_hash, action_hash(body.action_name, body.action))
    ):
        raise GatewayError(403, "action_mismatch", "Execution token does not match the action")
    if record.invalidated_at is not None or record.policy_version != workflow.policy_version:
        raise GatewayError(409, "policy_changed", "Policy changed; submit a fresh action check")
    if payload.get("policy_version", record.policy_version) != record.policy_version:
        raise GatewayError(403, "invalid_decision_token", "Policy binding is invalid")
    timeout_allow = (
        record.status == "expired" and record.timeout_outcome == "allow" and auth.risk_class < "R2"
    )
    if (record.status != "approved" and not timeout_allow) or record.consumed_at is not None:
        raise GatewayError(409, "approval_not_usable", "Approval is not usable")
    record.consumed_at = datetime.now(UTC)
    await append_event(
        session,
        AuditEventInput(
            auth.client_id,
            "action_consumed",
            usage={
                "workflow_id": str(auth.workflow_id),
                "action_hash": record.action_hash.hex(),
                "policy_version": record.policy_version,
                "matched_rules": record.matched_rules,
            },
            content_ref="approval:" + str(record.id),
        ),
    )
    await session.commit()
    return {"decision": "allow", "approval_id": str(record.id), "consumed": True}
