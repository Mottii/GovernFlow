import hmac
import os
import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from approvals.human import router as human_router
from approvals.service import Decision, decide
from core.database import get_session
from core.models import Budget, Client, KillFlag, Price, ProviderCredential, VirtualKey, Workflow
from core.security import encrypt_provider_key, generate_virtual_key, virtual_key_hash
from gateway.errors import GatewayError, openai_error_handler

app = FastAPI(title="Governed Automation Admin API")
app.add_exception_handler(GatewayError, openai_error_handler)
app.include_router(human_router)
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Admin token is not configured")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin token")


class ClientCreate(BaseModel):
    name: str = Field(min_length=1)
    retention_days: int = Field(default=180, ge=1)


class WorkflowCreate(BaseModel):
    client_id: uuid.UUID
    name: str = Field(min_length=1)
    risk_class: Literal["R0", "R1", "R2", "R3"]
    policy: dict[str, Any] = Field(default_factory=dict)


class BudgetCreate(BaseModel):
    workflow_id: uuid.UUID
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    cap_micro: int = Field(gt=0)


class CredentialCreate(BaseModel):
    client_id: uuid.UUID
    provider: Literal["openai", "anthropic", "gemini"]
    key: str = Field(min_length=1)
    label: str = Field(default="default", min_length=1)


class KeyCreate(BaseModel):
    workflow_id: uuid.UUID


class KillChange(BaseModel):
    scope: Literal["global", "client", "workflow"]
    scope_id: str = ""
    active: bool
    reason: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/clients", dependencies=[Depends(require_admin)], status_code=201)
async def create_client(body: ClientCreate, session: DbSession) -> dict[str, str | int]:
    client = Client(name=body.name, retention_days=body.retention_days)
    session.add(client)
    await session.commit()
    return {"id": str(client.id), "name": client.name, "retention_days": client.retention_days}


@app.post("/admin/workflows", dependencies=[Depends(require_admin)], status_code=201)
async def create_workflow(body: WorkflowCreate, session: DbSession) -> dict[str, str]:
    outage = body.policy.get("outage", {})
    if not isinstance(outage, dict) or outage.get("mode", "closed") not in ("open", "closed"):
        raise HTTPException(400, "Outage policy is invalid")
    if outage.get("mode") == "open" and body.risk_class not in ("R0", "R1"):
        raise HTTPException(400, "Fail open is available only for R0/R1 workflows")
    workflow = Workflow(
        client_id=body.client_id,
        name=body.name,
        risk_class=body.risk_class,
        policy=body.policy,
    )
    session.add(workflow)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Workflow could not be created") from exc
    return {"id": str(workflow.id), "client_id": str(workflow.client_id), "name": workflow.name}


@app.post("/admin/budgets", dependencies=[Depends(require_admin)], status_code=201)
async def create_budget(body: BudgetCreate, session: DbSession) -> dict[str, str | int]:
    budget = Budget(workflow_id=body.workflow_id, period=body.period, cap_micro=body.cap_micro)
    session.add(budget)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Budget could not be created") from exc
    return {"id": str(budget.id), "period": budget.period, "cap_micro": budget.cap_micro}


@app.post("/admin/provider-credentials", dependencies=[Depends(require_admin)], status_code=201)
async def create_provider_credential(body: CredentialCreate, session: DbSession) -> dict[str, str]:
    ciphertext, nonce = encrypt_provider_key(body.key, body.client_id, body.provider, body.label)
    credential = ProviderCredential(
        client_id=body.client_id,
        provider=body.provider,
        label=body.label,
        ciphertext=ciphertext,
        nonce=nonce,
    )
    session.add(credential)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Credential could not be created") from exc
    return {"id": str(credential.id), "provider": credential.provider, "label": credential.label}


@app.post("/admin/keys", dependencies=[Depends(require_admin)], status_code=201)
async def create_key(body: KeyCreate, session: DbSession) -> dict[str, str]:
    plaintext = generate_virtual_key()
    record = VirtualKey(
        workflow_id=body.workflow_id,
        key_hash=virtual_key_hash(plaintext),
        prefix=plaintext[:8],
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Virtual key could not be created") from exc
    return {"id": str(record.id), "key": plaintext, "prefix": record.prefix}


@app.delete("/admin/keys/{key_id}", dependencies=[Depends(require_admin)], status_code=204)
async def revoke_key(key_id: uuid.UUID, session: DbSession) -> None:
    record = await session.get(VirtualKey, key_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Virtual key not found")
    record.revoked_at = datetime.now(UTC)
    await session.commit()


@app.post("/admin/kill", dependencies=[Depends(require_admin)])
async def change_kill_flag(body: KillChange, session: DbSession) -> dict[str, str | bool]:
    if body.scope == "global" and body.scope_id:
        raise HTTPException(400, "Global scope_id must be empty")
    if body.scope != "global":
        try:
            uuid.UUID(body.scope_id)
        except ValueError as exc:
            raise HTTPException(400, "Client and workflow scope_id must be a UUID") from exc
    await session.execute(
        insert(KillFlag)
        .values(
            scope=body.scope,
            scope_id=body.scope_id,
            active=body.active,
            reason=body.reason,
        )
        .on_conflict_do_update(
            index_elements=[KillFlag.scope, KillFlag.scope_id],
            set_={"active": body.active, "reason": body.reason, "set_at": datetime.now(UTC)},
        )
    )
    await session.commit()
    return {"scope": body.scope, "scope_id": body.scope_id, "active": body.active}


@app.post("/admin/actions/{approval_id}/decision", dependencies=[Depends(require_admin)])
async def action_decision(
    approval_id: uuid.UUID, body: Decision, session: DbSession
) -> dict[str, Any]:
    return await decide(session, approval_id, body)


class PriceCreate(BaseModel):
    provider: Literal["openai", "anthropic", "gemini"]
    model: str = Field(min_length=1)
    effective_from: date
    input: int = Field(ge=0, le=10**12)
    output: int = Field(ge=0, le=10**12)
    cache_read: int = Field(default=0, ge=0, le=10**12)
    cache_write: int = Field(default=0, ge=0, le=10**12)
    cache_write_hour: int = Field(default=0, ge=0, le=10**12)
    reasoning: int = Field(default=0, ge=0, le=10**12)
    source_url: str = Field(pattern=r"^https://")
    meter: dict[str, Any] = Field(default_factory=dict)


@app.post("/admin/prices", dependencies=[Depends(require_admin)], status_code=201)
async def create_price(body: PriceCreate, session: DbSession) -> dict[str, str]:
    tier = body.meter.get("long_context")
    if tier:
        if (
            not isinstance(tier, dict)
            or not isinstance(tier.get("threshold_tokens"), int)
            or tier["threshold_tokens"] <= 0
        ):
            raise HTTPException(400, "Long-context price threshold is invalid")
        for name in ("input_multiplier", "output_multiplier"):
            if (
                not isinstance(tier.get(name), list)
                or len(tier[name]) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value <= 0
                    for value in tier[name]
                )
            ):
                raise HTTPException(
                    400, "Long-context multipliers must be positive integer fractions"
                )
    session.add(Price(**body.model_dump(), verified_at=datetime.now(UTC)))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Price effective date already exists") from exc
    return {"provider": body.provider, "model": body.model}


@app.post("/admin/prices/seed", dependencies=[Depends(require_admin)])
async def seed_prices(session: DbSession) -> dict[str, int]:
    from core.pricing import seed

    return {"models": await seed(session)}
