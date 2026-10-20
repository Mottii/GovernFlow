"""Durable local accounting queue. Only metadata is stored, never prompts or secrets."""

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def connect() -> sqlite3.Connection:
    path = Path(os.environ.get("REPLAY_QUEUE_PATH", "/tmp/gas-replay.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    return conn


def _enqueue(payload: dict[str, Any]) -> str:
    identity = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO queue VALUES (?, ?)", (identity, json.dumps(payload)))
    return identity


async def enqueue(payload: dict[str, Any]) -> str:
    return await asyncio.to_thread(_enqueue, payload)


async def replace(identity: str, payload: dict[str, Any]) -> None:
    def write() -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE queue SET payload = ? WHERE id = ?", (json.dumps(payload), identity)
            )

    await asyncio.to_thread(write)


async def replay(session: AsyncSession) -> int:
    from policy.recovery import recover_journal, recover_legacy

    return await recover_journal(session) + await recover_legacy(session)


async def main() -> None:
    from core.database import agency_sessions, session_factory
    from policy.recovery import retry_due

    async with session_factory() as session:
        total = await replay(session)
    async for session in agency_sessions():
        total += await retry_due(session)
    print(f"Replayed {total} accounting entries")


if __name__ == "__main__":
    asyncio.run(main())
