import hmac
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.models import Budget, Client, KillFlag, ProviderCredential, VirtualKey, Workflow
from core.security import encrypt_provider_key, generate_virtual_key, virtual_key_hash

app = FastAPI(title="Governed Automation Admin API")
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
