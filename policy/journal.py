"""Immutable accounting metadata in persistent SQLite or hosted S3 storage."""

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from policy.accounting import Evidence, EvidenceConflict


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class CallSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    budget_id: uuid.UUID
    run_id: uuid.UUID
    workflow_id: uuid.UUID
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    provider: str = Field(max_length=30)
    model: str = Field(max_length=200)
    estimate_micro: int = Field(gt=0, le=2**63 - 1)
    pricing: dict[str, Any]


class JournalRecord(Evidence):
    seed: CallSeed | None = None

    def evidence(self) -> Evidence:
        return Evidence.model_validate(self.model_dump(exclude={"seed"}))


class SQLiteJournal:
    def __init__(self) -> None:
        self.path = Path(os.environ.get("REPLAY_QUEUE_PATH", "/queue/replay.sqlite3"))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        os.chmod(self.path, 0o600)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
            "checksum BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER "
            "NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, error TEXT)"
        )
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def probe(self) -> None:
        def check() -> None:
            with self.connection() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS health (id INTEGER PRIMARY KEY, checked INTEGER)"
                )
                connection.execute(
                    "INSERT INTO health VALUES(1,1) ON CONFLICT(id) DO UPDATE SET checked=checked+1"
                )

        await asyncio.to_thread(check)

    async def write(self, payload: dict[str, Any]) -> None:
        raw = encode(payload)
        checksum = hashlib.sha256(raw).digest()

        def put() -> None:
            with self.connection() as connection:
                previous = connection.execute(
                    "SELECT checksum FROM evidence WHERE id=?", (payload["evidence_id"],)
                ).fetchone()
                if previous:
                    if previous[0] != checksum:
                        raise EvidenceConflict(
                            "Journal evidence identity conflicts with stored content"
                        )
                    return
                connection.execute(
                    "INSERT INTO evidence(id,payload,checksum) VALUES(?,?,?)",
                    (payload["evidence_id"], raw.decode(), checksum),
                )

        await asyncio.to_thread(put)

    async def entries(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        last = ""
        while True:

            def page(after: str = last) -> list[tuple[str, str]]:
                import time

                with self.connection() as connection:
                    return list(
                        connection.execute(
                            "SELECT id,payload FROM evidence WHERE id>? AND status='pending' "
                            "AND next_attempt<=? ORDER BY id LIMIT 100",
                            (after, time.time()),
                        )
                    )

            rows = await asyncio.to_thread(page)
            if not rows:
                break
            for identity, raw in rows:
                last = identity
                try:
                    yield identity, json.loads(raw)
                except (ValueError, TypeError):
                    await self.failed(identity, "invalid_json", quarantine=True)

    async def ack(self, identity: str, payload: dict[str, Any]) -> None:
        def remove() -> None:
            with self.connection() as connection:
                connection.execute(
                    "DELETE FROM evidence WHERE id=? AND checksum=?",
                    (identity, hashlib.sha256(encode(payload)).digest()),
                )

        await asyncio.to_thread(remove)

    async def failed(self, identity: str, error: str, *, quarantine: bool = False) -> None:
        def write() -> None:
            import time

            with self.connection() as connection:
                row = connection.execute(
                    "SELECT attempts FROM evidence WHERE id=?", (identity,)
                ).fetchone()
                if row:
                    attempts = row[0] + 1
                    connection.execute(
                        "UPDATE evidence SET attempts=?,status=?,next_attempt=?,error=? WHERE id=?",
                        (
                            attempts,
                            "quarantined" if quarantine or attempts >= 10 else "pending",
                            time.time() + min(900, 5 * 2 ** min(attempts, 8)),
                            error,
                            identity,
                        ),
                    )

        await asyncio.to_thread(write)


class S3Journal:
    def __init__(self) -> None:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        self.bucket = os.environ["RECOVERY_S3_BUCKET"]
        self.prefix = (
            os.environ.get("RECOVERY_S3_PREFIX", "governflow-recovery").strip("/") + "/v1/"
        )
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=os.environ.get("RECOVERY_S3_ENDPOINT"),
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def key(self, payload: dict[str, Any]) -> str:
        import uuid

        parts = [str(uuid.UUID(payload[name])) for name in ("agency_id", "call_id", "evidence_id")]
        return self.prefix + "/".join(parts) + ".json"

    async def probe(self) -> None:
        # Verify object-write permission, not only bucket visibility. Health objects have a
        # separate lifecycle prefix and never enter accounting replay.
        import uuid

        await self.operation(
            self.client.put_object,
            Bucket=self.bucket,
            Key=self.prefix.replace("/v1/", "/health/") + str(uuid.uuid4()),
            Body=b"{}",
            IfNoneMatch="*",
            ServerSideEncryption="AES256",
        )

    async def operation(self, fn: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except Exception:
            # SDK exceptions may include endpoint or credential details.
            raise OSError("Recovery object storage is unavailable") from None

    async def write(self, payload: dict[str, Any]) -> None:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        key, raw = self.key(payload), encode(payload)

        def put() -> None:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=raw,
                    IfNoneMatch="*",
                    ServerSideEncryption="AES256",
                    Metadata={"sha256": hashlib.sha256(raw).hexdigest()},
                )
            except ClientError as error:
                if error.response["Error"]["Code"] not in ("PreconditionFailed", "412"):
                    raise
                previous = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
                try:
                    content = previous.read(len(raw) + 1)
                finally:
                    previous.close()
                if content != raw:
                    raise EvidenceConflict(
                        "Journal evidence identity conflicts with stored content"
                    ) from None

        try:
            await asyncio.to_thread(put)
        except EvidenceConflict:
            raise
        except Exception:
            raise OSError("Recovery object storage is unavailable") from None

    async def entries(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        token = None
        while True:
            arguments: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": self.prefix,
                "MaxKeys": 100,
            }
            if token:
                arguments["ContinuationToken"] = token
            page = await self.operation(self.client.list_objects_v2, **arguments)
            for item in page.get("Contents", []):
                key = item["Key"]
                if item.get("Size", 0) > 65536:
                    continue  # Retain unsupported objects for operator inspection.
                response = await self.operation(self.client.get_object, Bucket=self.bucket, Key=key)
                try:
                    raw = await asyncio.to_thread(response["Body"].read, 65537)
                    payload = json.loads(raw)
                    if self.key(payload) != key:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
                finally:
                    response["Body"].close()
                yield key, payload
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]

    async def ack(self, identity: str, payload: dict[str, Any]) -> None:
        from botocore.exceptions import ClientError

        if identity != self.key(payload):
            raise EvidenceConflict("Journal object ownership does not match evidence")
        try:
            response = await asyncio.to_thread(
                self.client.get_object, Bucket=self.bucket, Key=identity
            )
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return
            raise OSError("Recovery object storage is unavailable") from None
        raw = encode(payload)
        try:
            content = await asyncio.to_thread(response["Body"].read, len(raw) + 1)
        finally:
            response["Body"].close()
        if content != raw:
            raise EvidenceConflict("Acknowledgement does not match stored evidence")
        await self.operation(
            self.client.delete_object, Bucket=self.bucket, Key=identity, IfMatch=response["ETag"]
        )

    async def failed(self, identity: str, error: str, *, quarantine: bool = False) -> None:
        pass  # Postgres jobs retain attempts and quarantine state; the object stays intact.


def journal() -> SQLiteJournal | S3Journal:
    backend = os.environ.get("RECOVERY_BACKEND", "sqlite")
    if os.environ.get("DEPLOYMENT_MODE", "self_hosted") == "hosted" and backend != "s3":
        raise RuntimeError("Hosted recovery requires S3")
    if backend == "s3":
        return S3Journal()
    if backend != "sqlite":
        raise RuntimeError("Unknown recovery backend")
    return SQLiteJournal()


async def persist(
    payload: dict[str, Any], *, store: SQLiteJournal | S3Journal | None = None
) -> None:
    if len(encode(payload)) > 65536:
        raise ValueError("Accounting evidence exceeds journal record limit")
    record = JournalRecord.model_validate_json(encode(payload))
    if record.kind in ("correction", "legacy"):
        raise EvidenceConflict("Administrator corrections cannot enter the recovery journal")
    target = store or journal()
    try:
        await target.write(record.model_dump(mode="json", exclude_none=True))
    finally:
        if store is None and isinstance(target, S3Journal):
            await asyncio.to_thread(target.client.close)
