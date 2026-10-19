import asyncio
import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select

from admin_api.app import app as admin_app
from core.models import CredentialVersion, ProviderAccount, ProviderCall, ProviderCredential
from core.rotation import eligible, preflight, reencrypt
from core.security import decrypt_provider_key

pytestmark = pytest.mark.asyncio


async def test_rotation_pins_inflight_call_and_retirement_blocks_future_calls(governed):
    auth = {"authorization": "Bearer test-admin"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin", headers=auth
    ) as admin:
        async with governed.factory() as session:
            slot = (
                await session.execute(
                    select(ProviderCredential).where(
                        ProviderCredential.client_id == governed.a.client.id,
                        ProviderCredential.provider == "openai",
                    )
                )
            ).scalar_one()
            identity = slot.id
        staged = await admin.post(
            f"/admin/provider-credentials/{identity}/versions",
            json={"key": "first-key", "expected_version": 0, "reason": "Initialize legacy fixture"},
        )
        assert staged.status_code == 201, staged.text
        assert "first-key" not in staged.text

        def activate(version, expected):
            return admin.post(
                f"/admin/provider-credentials/{identity}/versions/{version}/activate",
                json={"expected_active_version": expected, "reason": "Rotate"},
            )

        assert (await activate(1, 0)).status_code == 200
        async with governed.factory() as session:
            active = (
                await session.execute(
                    select(CredentialVersion).where(
                        CredentialVersion.credential_id == identity,
                        CredentialVersion.state == "active",
                    )
                )
            ).scalar_one()
            session.add(
                ProviderAccount(
                    client_id=governed.a.client.id,
                    provider="openai",
                    external_id="project-test",
                    validated_at=datetime.now(UTC),
                    validated_credential_version_id=active.id,
                )
            )
            await session.commit()
        started = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def provider(request):
            seen.append(request.headers["authorization"])
            started.set()
            await release.wait()
            return httpx.Response(200, json={"usage": {"prompt_tokens": 2, "completion_tokens": 1}})

        with respx.mock as mock:
            mock.post("http://provider.test/v1/chat/completions").mock(side_effect=provider)
            first = asyncio.create_task(
                governed.http.post(
                    "/v1/chat/completions",
                    headers={"authorization": "Bearer " + governed.a.key, "x-run-id": "first"},
                    json={"model": "gpt-mock", "max_tokens": 16},
                )
            )
            await started.wait()
            assert (
                await admin.post(
                    f"/admin/provider-credentials/{identity}/versions",
                    json={"key": "second-key", "expected_version": 1, "reason": "Replacement"},
                )
            ).status_code == 201
            assert (await activate(2, 1)).status_code == 200
            async with governed.factory() as session:
                account = await session.get(ProviderAccount, (governed.a.client.id, "openai"))
                assert account.validated_at is None
                assert account.validated_credential_version_id is None
            release.set()
            assert (await first).status_code == 200
            second = await governed.http.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer " + governed.a.key, "x-run-id": "second"},
                json={"model": "gpt-mock", "max_tokens": 16},
            )
            assert second.status_code == 200
            assert seen == ["Bearer first-key", "Bearer second-key"]
        retired = await admin.post(
            f"/admin/provider-credentials/{identity}/versions/2/retire",
            json={"expected_active_version": 2, "reason": "Disable slot"},
        )
        assert retired.status_code == 200
        # Metadata-only fixture mode must not fall back after a version was explicitly retired.
        blocked = await governed.http.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer " + governed.a.key},
            json={"model": "gpt-mock", "max_tokens": 16},
        )
        assert blocked.status_code == 503
        metadata = await admin.get("/admin/provider-credentials")
        assert "first-key" not in metadata.text and "second-key" not in metadata.text
    async with governed.factory() as session:
        calls = list((await session.execute(select(ProviderCall))).scalars())
        assert len({c.credential_version_id for c in calls}) == 2


async def test_reencryption_is_resumable_preserves_plaintext_and_protects_retirement(
    governed, monkeypatch
):
    target = base64.b64encode(b"z" * 32).decode()
    monkeypatch.setenv("ENCRYPTION_KEYS", json.dumps({"0001": target}))
    monkeypatch.setenv("ACTIVE_ENCRYPTION_KEY_ID", "0001")
    async with governed.factory() as session:
        before = await preflight(session)
        assert before["ciphertext_references"]["0000"] > 0
        assert not await eligible(session, "encryption", "0000")
        changed = 0
        while True:
            count = await reencrypt(session, "test operator", 2)
            changed += count
            if count == 0:
                break
        assert changed == 6
        assert await eligible(session, "encryption", "0000")
        slot = (
            await session.execute(
                select(ProviderCredential).where(
                    ProviderCredential.client_id == governed.a.client.id,
                    ProviderCredential.provider == "openai",
                )
            )
        ).scalar_one()
        assert slot.encryption_key_id == "0001"
        assert (
            decrypt_provider_key(
                slot.ciphertext,
                slot.nonce,
                slot.client_id,
                slot.provider,
                slot.label,
                key_id=slot.encryption_key_id,
            )
            == "provider-A"
        )
        monkeypatch.delenv("MASTER_ENCRYPTION_KEY")
        assert (await preflight(session))["active_encryption_key_id"] == "0001"


async def test_signing_key_retention_and_key_id_material_reuse_are_enforced(governed, monkeypatch):
    from datetime import timedelta

    from core.models import DeploymentKey
    from core.rotation import observe
    from policy.accounting import database_now

    async with governed.factory() as session:
        await observe(session)
        monkeypatch.setenv(
            "SIGNING_KEYS", json.dumps({"0001": base64.b64encode(b"v" * 32).decode()})
        )
        monkeypatch.setenv("ACTIVE_SIGNING_KEY_ID", "0001")
        # Even before a rotation observation, the last active verification key is protected.
        monkeypatch.delenv("DECISION_SIGNING_KEY")
        with pytest.raises(RuntimeError, match="retained key IDs"):
            await preflight(session)
        monkeypatch.setenv("DECISION_SIGNING_KEY", base64.b64encode(b"d" * 32).decode())
        await observe(session)
        assert not await eligible(session, "signing", "0000")
        monkeypatch.delenv("DECISION_SIGNING_KEY")
        with pytest.raises(RuntimeError, match="retained key IDs"):
            await preflight(session)
        monkeypatch.setenv("DECISION_SIGNING_KEY", base64.b64encode(b"d" * 32).decode())
        old = await session.get(DeploymentKey, ("signing", "0000"))
        old.verify_until = (await database_now(session)) - timedelta(seconds=1)
        await session.commit()
        assert await eligible(session, "signing", "0000")
        monkeypatch.setenv(
            "SIGNING_KEYS", json.dumps({"0001": base64.b64encode(b"x" * 32).decode()})
        )
        with pytest.raises(RuntimeError, match="different material"):
            await preflight(session)
