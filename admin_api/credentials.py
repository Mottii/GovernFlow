"""Credential slot metadata and versioned replacement without plaintext reads."""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select, update

from admin_api.auth import Actor, DbSession, client_scope, permit, permitted_clients
from audit.administration import record
from core.keyrings import active_id
from core.models import CredentialVersion, ProviderAccount, ProviderCredential
from core.security import encrypt_provider_key

router = APIRouter()


class Replacement(BaseModel):
    key: SecretStr
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=200)


class VersionChange(BaseModel):
    expected_active_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=200)


async def slot(session: DbSession, actor: Actor, identity: uuid.UUID) -> ProviderCredential:
    item = await session.get(ProviderCredential, identity)
    if item is None:
        raise HTTPException(404, "Credential not found")
    await client_scope(session, actor, item.client_id)
    return (
        await session.execute(
            select(ProviderCredential)
            .where(ProviderCredential.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def add_version(
    session: DbSession, item: ProviderCredential, plaintext: str, actor: str, state: str = "staged"
) -> CredentialVersion:
    if not plaintext or len(plaintext) > 16384:
        raise HTTPException(422, "Credential must contain 1–16384 characters")
    version = (
        await session.execute(
            select(func.coalesce(func.max(CredentialVersion.version), 0)).where(
                CredentialVersion.credential_id == item.id
            )
        )
    ).scalar_one() + 1
    key_id = active_id("encryption")
    ciphertext, nonce = encrypt_provider_key(
        plaintext, item.client_id, item.provider, item.label, key_id=key_id
    )
    result = CredentialVersion(
        agency_id=item.agency_id,
        credential_id=item.id,
        version=version,
        state=state,
        ciphertext=ciphertext,
        nonce=nonce,
        encryption_key_id=key_id,
        actor=actor,
    )
    session.add(result)
    await session.flush()
    if state == "active":
        item.active_version_id = result.id
    return result


def version_view(item: CredentialVersion) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "version": item.version,
        "state": item.state,
        "key_id": item.encryption_key_id or "0000",
        "created_at": item.created_at.isoformat(),
        "author": item.actor,
    }


@router.get("/admin/provider-credentials")
async def listing(session: DbSession, actor: Actor) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ProviderCredential).where(
                ProviderCredential.client_id.in_(permitted_clients(actor))
            )
        )
    ).scalars()
    results = []
    for item in rows:
        versions = list(
            (
                await session.execute(
                    select(CredentialVersion)
                    .where(CredentialVersion.credential_id == item.id)
                    .order_by(CredentialVersion.version.desc())
                )
            ).scalars()
        )
        results.append(
            {
                "id": str(item.id),
                "client_id": str(item.client_id),
                "provider": item.provider,
                "label": item.label,
                "active_version_id": str(item.active_version_id)
                if item.active_version_id
                else None,
                "active_version": next(
                    (v.version for v in versions if v.id == item.active_version_id), 0
                ),
                "versions": [version_view(v) for v in versions],
            }
        )
    return results


@router.post("/admin/provider-credentials/{credential_id}/versions", status_code=201)
async def replacement(
    credential_id: uuid.UUID, body: Replacement, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    item = await slot(session, actor, credential_id)
    current = (
        await session.execute(
            select(func.coalesce(func.max(CredentialVersion.version), 0)).where(
                CredentialVersion.credential_id == item.id
            )
        )
    ).scalar_one()
    if body.expected_version != current:
        raise HTTPException(409, {"version": current})
    result = await add_version(session, item, body.key.get_secret_value(), actor.email)
    await record(
        session,
        actor.agency_id,
        actor.email,
        "credential",
        item.id,
        "version_staged",
        version=result.version,
        reason=body.reason,
    )
    await session.commit()
    return version_view(result)


@router.post("/admin/provider-credentials/{credential_id}/versions/{version}/activate")
async def activate(
    credential_id: uuid.UUID, version: int, body: VersionChange, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    item = await slot(session, actor, credential_id)
    current = (
        await session.get(CredentialVersion, item.active_version_id)
        if item.active_version_id
        else None
    )
    if (current.version if current else 0) != body.expected_active_version:
        raise HTTPException(409, "Active credential changed")
    result = (
        await session.execute(
            select(CredentialVersion).where(
                CredentialVersion.credential_id == item.id, CredentialVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if result is None:
        raise HTTPException(404, "Version not found")
    if result.state != "staged":
        raise HTTPException(409, "Only staged versions can be activated")
    if current:
        current.state = "retired"
        await session.flush()
    result.state = "active"
    item.active_version_id = result.id
    await session.execute(
        update(ProviderAccount)
        .where(
            ProviderAccount.client_id == item.client_id,
            ProviderAccount.provider == item.provider,
            ProviderAccount.credential_label == item.label,
        )
        .values(validated_at=None, validated_credential_version_id=None)
    )
    await record(
        session,
        actor.agency_id,
        actor.email,
        "credential",
        item.id,
        "version_activated",
        version=version,
        reason=body.reason,
    )
    await session.commit()
    return version_view(result)


@router.post("/admin/provider-credentials/{credential_id}/versions/{version}/retire")
async def retire(
    credential_id: uuid.UUID, version: int, body: VersionChange, session: DbSession, actor: Actor
) -> dict[str, Any]:
    permit(actor, "admin", "operator")
    item = await slot(session, actor, credential_id)
    current = (
        await session.get(CredentialVersion, item.active_version_id)
        if item.active_version_id
        else None
    )
    if (current.version if current else 0) != body.expected_active_version:
        raise HTTPException(409, "Active credential changed")
    result = (
        await session.execute(
            select(CredentialVersion).where(
                CredentialVersion.credential_id == item.id, CredentialVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if result is None:
        raise HTTPException(404, "Version not found")
    result.state = "retired"
    if item.active_version_id == result.id:
        item.active_version_id = None
    await record(
        session,
        actor.agency_id,
        actor.email,
        "credential",
        item.id,
        "version_retired",
        version=version,
        reason=body.reason,
    )
    await session.commit()
    return version_view(result)
