import os
import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from admin_api.agencies import router as agencies_router
from admin_api.auth import Actor, DbSession, client_scope, permit, platform_only, workflow_scope
from admin_api.dashboard import router as dashboard_router
from admin_api.reports import router as reports_router
from admin_api.users import router as users_router
from approvals.human import router as human_router
from approvals.service import Decision, decide
from core.models import (
    Approval,
    Budget,
    Client,
    KillFlag,
    Price,
    ProviderAccount,
    ProviderCredential,
    VirtualKey,
    Workflow,
)
from core.security import encrypt_provider_key, generate_virtual_key, virtual_key_hash
from gateway.errors import GatewayError, openai_error_handler

app = FastAPI(title="Governed Automation Admin API")
app.add_exception_handler(GatewayError, openai_error_handler)
app.include_router(human_router)
app.include_router(users_router)
app.include_router(agencies_router)
app.include_router(dashboard_router)
app.include_router(reports_router)


@app.middleware("http")
async def no_cache(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(DBAPIError)
async def storage_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "Governance storage is unavailable"})


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


@app.post("/admin/clients", status_code=201)
async def create_client(
    body: ClientCreate, session: DbSession, actor: Actor
) -> dict[str, str | int]:
    permit(actor, "admin")
    client = Client(agency_id=actor.agency_id, name=body.name, retention_days=body.retention_days)
    session.add(client)
    await session.commit()
    return {"id": str(client.id), "name": client.name, "retention_days": client.retention_days}


@app.post("/admin/workflows", status_code=201)
async def create_workflow(body: WorkflowCreate, session: DbSession, actor: Actor) -> dict[str, str]:
    permit(actor, "admin", "operator")
    await client_scope(session, actor, body.client_id)
    outage = body.policy.get("outage", {})
    if not isinstance(outage, dict) or outage.get("mode", "closed") not in ("open", "closed"):
        raise HTTPException(400, "Outage policy is invalid")
    if outage.get("mode") == "open":
        permit(actor, "admin")
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


@app.post("/admin/budgets", status_code=201)
async def create_budget(
    body: BudgetCreate, session: DbSession, actor: Actor
) -> dict[str, str | int]:
    permit(actor, "admin", "operator")
    await workflow_scope(session, actor, body.workflow_id)
    budget = Budget(workflow_id=body.workflow_id, period=body.period, cap_micro=body.cap_micro)
    session.add(budget)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Budget could not be created") from exc
    return {"id": str(budget.id), "period": budget.period, "cap_micro": budget.cap_micro}


@app.post("/admin/provider-credentials", status_code=201)
async def create_provider_credential(
    body: CredentialCreate, session: DbSession, actor: Actor
) -> dict[str, str]:
    permit(actor, "admin", "operator")
    await client_scope(session, actor, body.client_id)
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


@app.post("/admin/keys", status_code=201)
async def create_key(body: KeyCreate, session: DbSession, actor: Actor) -> dict[str, str]:
    permit(actor, "admin", "operator")
    await workflow_scope(session, actor, body.workflow_id)
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


@app.delete("/admin/keys/{key_id}", status_code=204)
async def revoke_key(key_id: uuid.UUID, session: DbSession, actor: Actor) -> None:
    record = await session.get(VirtualKey, key_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Virtual key not found")
    permit(actor, "admin", "operator")
    await workflow_scope(session, actor, record.workflow_id)
    record.revoked_at = datetime.now(UTC)
    await session.commit()


@app.post("/admin/kill")
async def change_kill_flag(
    body: KillChange, session: DbSession, actor: Actor
) -> dict[str, str | bool]:
    permit(actor, "admin", "operator")
    if body.scope == "global":
        platform_only(actor)
    if body.scope == "global" and body.scope_id:
        raise HTTPException(400, "Global scope_id must be empty")
    if body.scope != "global":
        try:
            body = body.model_copy(update={"scope_id": str(uuid.UUID(body.scope_id))})
        except ValueError as exc:
            raise HTTPException(400, "Client and workflow scope_id must be a UUID") from exc
    if body.scope == "client":
        await client_scope(session, actor, uuid.UUID(body.scope_id))
    elif body.scope == "workflow":
        await workflow_scope(session, actor, uuid.UUID(body.scope_id))
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


@app.post("/admin/actions/{approval_id}/decision")
async def action_decision(
    approval_id: uuid.UUID, body: Decision, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "approver")
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "Approval not found")
    await workflow_scope(session, actor, approval.workflow_id)
    if actor.user_id:
        body = body.model_copy(update={"approver": actor.email})
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


@app.post("/admin/prices", status_code=201)
async def create_price(body: PriceCreate, session: DbSession, actor: Actor) -> dict[str, str]:
    platform_only(actor)
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


@app.post("/admin/prices/seed")
async def seed_prices(session: DbSession, actor: Actor) -> dict[str, int]:
    platform_only(actor)
    from core.pricing import seed

    return {"models": await seed(session)}


class AccountCreate(BaseModel):
    client_id: uuid.UUID
    provider: Literal["openai", "anthropic"]
    external_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    credential_label: str = "default"
    inference_key_id: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    validate_live: bool = True


@app.post("/admin/provider-accounts", status_code=201)
async def create_account(body: AccountCreate, session: DbSession, actor: Actor) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    await client_scope(session, actor, body.client_id)
    import httpx

    validated = None
    if body.validate_live:
        key = os.environ.get(body.provider.upper() + "_ADMIN_KEY")
        if not key:
            raise HTTPException(503, "Provider reporting key is required to validate the account")
        if body.provider == "anthropic" and not body.inference_key_id:
            raise HTTPException(400, "Anthropic inference API key ID is required")
        base = os.environ.get(
            body.provider.upper() + "_REPORTING_BASE_URL",
            "https://api.openai.com" if body.provider == "openai" else "https://api.anthropic.com",
        ).rstrip("/")
        path = (
            "/v1/organization/projects/" + body.external_id
            if body.provider == "openai"
            else "/v1/organizations/api_keys/" + str(body.inference_key_id)
        )
        headers = (
            {"authorization": "Bearer " + key}
            if body.provider == "openai"
            else {"x-api-key": key, "anthropic-version": "2023-06-01"}
        )
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                response = await http.get(base + path, headers=headers)
                response.raise_for_status()
                if body.provider == "openai" and (
                    response.json().get("id") != body.external_id
                    or response.json().get("status") == "archived"
                ):
                    raise HTTPException(400, "OpenAI project is invalid or archived")
                if (
                    body.provider == "anthropic"
                    and response.json().get("workspace_id") != body.external_id
                ):
                    raise HTTPException(
                        400, "Inference key does not belong to the mapped workspace"
                    )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Provider account validation failed") from exc
        validated = datetime.now(UTC)
    session.add(
        ProviderAccount(**body.model_dump(exclude={"validate_live"}), validated_at=validated)
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, "Provider account must be dedicated to exactly one client"
        ) from exc
    return {
        "provider": body.provider,
        "external_id": body.external_id,
        "validated": validated is not None,
    }
