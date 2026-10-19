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
from core.models import Approval, IntegrationDestination, Notification, Workflow
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
    if record.invalidated_at is not None:
        return {
            "approval_id": str(record.id),
            "status": record.status,
            "decision": "deny",
            "reason": "policy_changed",
            "policy_version": record.policy_version,
            "matched_rules": record.matched_rules,
            "expires_at": record.expires_at.isoformat(),
        }
    result: dict[str, Any] = {
        "policy_version": record.policy_version,
        "matched_rules": record.matched_rules,
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
                    "policy_version": record.policy_version,
                    "matched_rules": record.matched_rules,
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
                record.content_ciphertext,
                record.content_nonce,
                client_id,
                "action",
                str(record.id),
                key_id=record.encryption_key_id,
            )
        ),
    )


async def check(session: AsyncSession, auth: AuthContext, body: ActionCheck) -> dict[str, Any]:
    await check_kill(session, auth)
    from policy.revisions import evaluate, lock

    workflow = await lock(session, auth.workflow_id)
    try:
        evaluation = evaluate(workflow.policy, workflow.risk_class, body.action_name, body.action)
    except (ValueError, TypeError) as error:
        raise GatewayError(503, "invalid_policy", "Workflow policy requires review") from error
    if evaluation["decision"] == "deny":
        await append_event(
            session,
            AuditEventInput(
                auth.client_id,
                "action_denied",
                usage={
                    "workflow_id": str(auth.workflow_id),
                    "action_name": body.action_name,
                    "reason": evaluation["reason"],
                    "policy_version": workflow.policy_version,
                    "matched_rules": evaluation["matched_rules"],
                },
                content_ref="action-hash:" + action_hash(body.action_name, body.action).hex(),
            ),
        )
        await session.commit()
        return {
            "decision": "deny",
            "status": "denied",
            "reason": evaluation["reason"],
            "policy_version": workflow.policy_version,
            "matched_rules": evaluation["matched_rules"],
        }
    risk = evaluation["risk_class"]
    timeout, outcome = evaluation["timeout_seconds"], evaluation["on_timeout"]
    automatic = evaluation["decision"] == "allow"
    now = datetime.now(UTC)
    record = Approval(
        id=uuid.uuid4(),
        policy_version=workflow.policy_version,
        matched_rules=evaluation["matched_rules"],
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
    from core.keyrings import active_id

    record.encryption_key_id = active_id("encryption")
    record.content_ciphertext, record.content_nonce = encrypt_provider_key(
        json.dumps(body.action, allow_nan=False), auth.client_id, "action", str(record.id)
    )
    session.add(record)
    await append_event(
        session,
        AuditEventInput(
            auth.client_id,
            "action_proposed",
            usage={
                "workflow_id": str(auth.workflow_id),
                "risk_class": risk,
                "policy_version": record.policy_version,
                "matched_rules": record.matched_rules,
                "action_name": body.action_name,
                "action_hash": record.action_hash.hex(),
            },
            content_ref="approval:" + str(record.id),
        ),
    )
    if record.status == "approved":
        await append_event(
            session,
            AuditEventInput(
                auth.client_id,
                "action_approved",
                usage={
                    "workflow_id": str(auth.workflow_id),
                    "approver": "policy",
                    "policy_version": record.policy_version,
                    "matched_rules": record.matched_rules,
                    "action_hash": record.action_hash.hex(),
                },
                content_ref="approval:" + str(record.id),
            ),
        )
    if record.status == "pending":
        channels = workflow.policy.get("approval_channels", ["telegram", "email", "webhook"])
        if not isinstance(channels, list) or any(
            channel not in ("telegram", "email", "webhook") for channel in channels
        ):
            raise GatewayError(503, "invalid_policy", "Approval notification channels are invalid")
        from approvals.reviewers import destination_matches

        await session.flush()
        assert workflow is not None
        destinations = (
            await session.execute(
                select(IntegrationDestination).where(
                    IntegrationDestination.agency_id == auth.agency_id,
                    IntegrationDestination.client_id == auth.client_id,
                    IntegrationDestination.enabled.is_(True),
                    IntegrationDestination.channel.in_(channels),
                )
            )
        ).scalars()
        for destination in destinations:
            if await destination_matches(session, destination, workflow):
                session.add(
                    Notification(
                        approval_id=record.id,
                        channel=destination.channel,
                        destination_id=destination.id,
                    )
                )
    await session.commit()
    return view(record)


async def scoped(session: AsyncSession, auth: AuthContext, approval_id: uuid.UUID) -> Approval:
    from policy.revisions import lock

    workflow = await lock(session, auth.workflow_id)
    record = (
        await session.execute(
            select(Approval).where(
                Approval.id == approval_id, Approval.workflow_id == auth.workflow_id
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    if record.policy_version != workflow.policy_version and record.consumed_at is None:
        record.invalidated_at = datetime.now(UTC)
    return record


async def decide(session: AsyncSession, approval_id: uuid.UUID, body: Decision) -> dict[str, Any]:
    from policy.revisions import lock

    initial = await session.get(Approval, approval_id)
    if initial is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    workflow = await lock(session, initial.workflow_id)
    record = (
        await session.execute(
            select(Approval)
            .where(Approval.id == approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if record is None:
        raise GatewayError(404, "approval_not_found", "Approval was not found")
    if record.invalidated_at is not None or record.policy_version != workflow.policy_version:
        raise GatewayError(409, "policy_changed", "Policy changed; submit a fresh action check")
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
            client_id,
            "action_" + record.status,
            usage={
                "workflow_id": str(record.workflow_id),
                "approver": record.approver,
                "policy_version": record.policy_version,
                "matched_rules": record.matched_rules,
                "action_hash": record.action_hash.hex(),
            },
            content_ref="approval:" + str(record.id),
        ),
    )
    await session.commit()
    return view(record)
