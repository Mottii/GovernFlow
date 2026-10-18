"""S3 contract against an isolated S3 emulator; never uses ambient AWS credentials."""

import os
import uuid

import pytest
from sqlalchemy import select

from core.models import Budget, ChargeEntry
from policy.accounting import EvidenceConflict
from policy.journal import S3Journal
from policy.recovery import recover_journal
from tests.test_accounting import pending_call


@pytest.fixture
async def s3_store(monkeypatch):
    endpoint = os.environ.get("S3_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("S3_TEST_ENDPOINT is required for emulator acceptance")
    from urllib.parse import urlsplit

    if urlsplit(endpoint).hostname not in ("127.0.0.1", "localhost", "moto"):
        pytest.fail("S3 tests require an explicit local emulator endpoint")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "phase2_fixture")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "phase2_fixture_password")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("RECOVERY_BACKEND", "s3")
    monkeypatch.setenv("RECOVERY_S3_ENDPOINT", endpoint)
    bucket = "governflow-test-" + uuid.uuid4().hex
    monkeypatch.setenv("RECOVERY_S3_BUCKET", bucket)
    store = S3Journal()
    store.client.create_bucket(Bucket=bucket)
    try:
        await store.probe()
        yield store
    finally:
        objects = store.client.list_objects_v2(Bucket=bucket).get("Contents", [])
        for item in objects:
            store.client.delete_object(Bucket=bucket, Key=item["Key"])
        store.client.delete_bucket(Bucket=bucket)
        store.client.close()


async def test_conditional_s3_write_survives_reinstantiation_and_deduplicated_replay(
    governed, s3_store
):
    held, evidence = await pending_call(governed)
    payload = evidence.model_dump(mode="json")
    await s3_store.write(payload)
    await s3_store.write(payload)
    with pytest.raises(EvidenceConflict):
        await s3_store.write({**payload, "amount_micro": 16})
    replacement = S3Journal()
    try:
        entries = [entry async for entry in replacement.entries()]
        assert len(entries) == 1 and entries[0][1] == payload
    finally:
        replacement.client.close()
    async with governed.factory() as session:
        assert await recover_journal(session) == 1
        assert await recover_journal(session) == 0
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15
        assert len((await session.execute(select(ChargeEntry))).scalars().all()) == 1
    assert [entry async for entry in s3_store.entries()] == []


async def test_s3_unavailable_or_corrupt_objects_are_not_acknowledged(
    governed, s3_store, monkeypatch
):
    held, evidence = await pending_call(governed)
    payload = evidence.model_dump(mode="json")
    await s3_store.write(payload)
    original = S3Journal.ack

    async def unavailable(*args, **kwargs):
        raise OSError("simulated storage outage after database commit")

    monkeypatch.setattr(S3Journal, "ack", unavailable)
    async with governed.factory() as session:
        assert await recover_journal(session) == 0
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15
    assert len([entry async for entry in s3_store.entries()]) == 1
    monkeypatch.setattr(S3Journal, "ack", original)
    async with governed.factory() as session:
        assert await recover_journal(session) == 1
        assert (await session.get(Budget, held.budget_id)).spent_micro == 15
    corrupt = s3_store.prefix + f"{evidence.agency_id}/{evidence.call_id}/{uuid.uuid4()}.json"
    s3_store.client.put_object(Bucket=s3_store.bucket, Key=corrupt, Body=b"bad json")
    async with governed.factory() as session:
        assert await recover_journal(session) == 0
    assert s3_store.client.head_object(Bucket=s3_store.bucket, Key=corrupt)["ContentLength"] == 8
