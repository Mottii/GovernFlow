"""Maintenance-window preflight, resumable re-encryption and key retirement checks."""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from audit.administration import record
from core.keyrings import Kind, active_id, ring
from core.models import (
    Agency,
    Approval,
    CredentialVersion,
    DeploymentKey,
    IntegrationDestination,
    ProviderCredential,
    Workflow,
)
from core.security import decrypt_provider_key, encrypt_provider_key
from policy.accounting import database_now


async def references(session: AsyncSession) -> dict[str, int]:
    result: dict[str, int] = {}
    for model, column in (
        (ProviderCredential, ProviderCredential.ciphertext),
        (CredentialVersion, CredentialVersion.ciphertext),
        (Approval, Approval.content_ciphertext),
        (IntegrationDestination, IntegrationDestination.secret_ciphertext),
    ):
        rows = await session.execute(
            select(model.encryption_key_id, func.count())
            .where(column.is_not(None))
            .group_by(model.encryption_key_id)
        )
        for identity, count in rows:
            name = identity or "0000"
            result[name] = result.get(name, 0) + count
    return result


async def preflight(session: AsyncSession) -> dict[str, Any]:
    required = await references(session)
    missing = set(required) - set(ring("encryption"))
    now = await database_now(session)
    protected = (
        await session.execute(
            select(DeploymentKey).where(
                DeploymentKey.kind == "signing",
                or_(DeploymentKey.verify_until > now, DeploymentKey.retired_at.is_(None)),
            )
        )
    ).scalars()
    for item in protected:
        if item.key_id not in ring("signing"):
            missing.add("signing:" + item.key_id)
    for kind in ("encryption", "signing"):
        keys = ring(kind)
        for item in (
            await session.execute(select(DeploymentKey).where(DeploymentKey.kind == kind))
        ).scalars():
            if (
                item.key_id in keys
                and item.fingerprint
                and hashlib.sha256(keys[item.key_id]).hexdigest() != item.fingerprint
            ):
                raise RuntimeError("A deployment key ID was reused with different material")
    if missing:
        raise RuntimeError(
            "Required retained key IDs are unavailable: " + ",".join(sorted(missing))
        )
    return {
        "active_encryption_key_id": active_id("encryption"),
        "active_signing_key_id": active_id("signing"),
        "ciphertext_references": required,
    }


async def observe(session: AsyncSession, actor: str = "initialization") -> None:
    now = await database_now(session)
    agencies = list((await session.execute(select(Agency.id))).scalars())
    for kind in ("encryption", "signing"):
        active = active_id(kind)
        for identity, key in ring(kind).items():
            item = await session.get(DeploymentKey, (kind, identity))
            changed = item is None or not item.fingerprint
            if item is None:
                item = DeploymentKey(
                    kind=kind, key_id=identity, fingerprint=hashlib.sha256(key).hexdigest()
                )
                session.add(item)
            elif item.fingerprint and item.fingerprint != hashlib.sha256(key).hexdigest():
                raise RuntimeError("Deployment key ID material changed")
            item.fingerprint = hashlib.sha256(key).hexdigest()
            if identity == active:
                changed = changed or item.retired_at is not None
                item.retired_at, item.verify_until = None, None
            elif item.retired_at is None:
                changed = True
                item.retired_at = now
                item.verify_until = now + timedelta(days=7, minutes=5) if kind == "signing" else now
            if changed:
                for agency in agencies:
                    await record(
                        session,
                        agency,
                        actor,
                        "deployment_key",
                        kind + ":" + identity,
                        "state_observed",
                        key_id=identity,
                        scope=kind,
                        active=identity == active,
                    )
    await session.commit()


async def reencrypt(session: AsyncSession, actor: str, batch: int) -> int:
    target = active_id("encryption")
    count = 0
    models: list[Any] = [ProviderCredential, CredentialVersion, Approval, IntegrationDestination]
    for model in models:
        field = (
            model.content_ciphertext
            if model is Approval
            else model.secret_ciphertext
            if model is IntegrationDestination
            else model.ciphertext
        )
        rows: list[Any] = list(
            (
                await session.execute(
                    select(model)
                    .where(
                        field.is_not(None),
                        func.coalesce(model.encryption_key_id, "0000") != target,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(batch - count)
                )
            ).scalars()
        )
        for item in rows:
            if model is CredentialVersion:
                parent = await session.get(ProviderCredential, item.credential_id)
                assert parent is not None
                owner, provider, label = parent.client_id, parent.provider, parent.label
                ciphertext, nonce = item.ciphertext, item.nonce
            elif model is Approval:
                workflow = await session.get(Workflow, item.workflow_id)
                assert workflow is not None
                owner, provider, label = workflow.client_id, "action", str(item.id)
                ciphertext, nonce = item.content_ciphertext, item.content_nonce
            elif model is IntegrationDestination:
                owner, provider, label = item.agency_id, "integration", str(item.id)
                ciphertext, nonce = item.secret_ciphertext, item.secret_nonce
            else:
                owner, provider, label = item.client_id, item.provider, item.label
                ciphertext, nonce = item.ciphertext, item.nonce
            plain = decrypt_provider_key(
                ciphertext, nonce, owner, provider, label, key_id=item.encryption_key_id
            )
            encrypted, nonce = encrypt_provider_key(plain, owner, provider, label, key_id=target)
            if model is Approval:
                item.content_ciphertext, item.content_nonce = encrypted, nonce
            elif model is IntegrationDestination:
                item.secret_ciphertext, item.secret_nonce = encrypted, nonce
            else:
                item.ciphertext, item.nonce = encrypted, nonce
            item.encryption_key_id = target
            await record(
                session, item.agency_id, actor, "encryption", item.id, "reencrypted", key_id=target
            )
            count += 1
        if count >= batch:
            break
    await session.commit()
    return count


async def eligible(session: AsyncSession, kind: str, identity: str) -> bool:
    if identity == active_id(cast(Kind, kind)):
        return False
    if kind == "encryption":
        return (await references(session)).get(identity, 0) == 0
    item = await session.get(DeploymentKey, (kind, identity))
    return bool(item and item.verify_until and item.verify_until <= await database_now(session))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "observe", "reencrypt", "eligible"))
    parser.add_argument("--operator", default="maintenance")
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--kind", choices=("encryption", "signing"), default="encryption")
    parser.add_argument("--key-id")
    args = parser.parse_args()
    if os.environ.get("MAINTENANCE_MODE") != "true":
        parser.error("Use an explicit owner maintenance process")
    if not 1 <= args.batch <= 1000:
        parser.error("Batch must be 1–1000")
    from core.database import session_factory

    async with session_factory() as session:
        if args.operation == "preflight":
            print(json.dumps(await preflight(session)))
        elif args.operation == "observe":
            await preflight(session)
            await observe(session, args.operator)
            print(
                "Key identifiers recorded; retain retired signing keys for seven "
                "days plus five minutes"
            )
        elif args.operation == "reencrypt":
            print(json.dumps({"reencrypted": await reencrypt(session, args.operator, args.batch)}))
        else:
            if not args.key_id:
                parser.error("eligible requires --key-id")
            if not await eligible(session, args.kind, args.key_id):
                raise SystemExit("Key retirement is not yet safe")
            print("Key retirement is eligible; update deployment secrets during maintenance")


if __name__ == "__main__":
    asyncio.run(main())
