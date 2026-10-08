"""Durable local accounting queue. Only metadata is stored, never prompts or secrets."""

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput, append_event
from core.database import set_agency_context
from core.models import DEFAULT_AGENCY_ID, Budget, ReplayReceipt, Run
from policy.budget import HeldReservation, settle


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
    def pending() -> list[tuple[str, str]]:
        with connect() as conn:
            return list(conn.execute("SELECT id, payload FROM queue"))

    rows = await asyncio.to_thread(pending)
    done = 0
    for identity, raw in rows:
        payload = json.loads(raw)
        if (
            not payload.get("ready", True)
            and datetime.now(UTC).timestamp() - payload["queued_at"] < 3600
        ):
            continue
        agency_id = uuid.UUID(payload.get("agency_id", str(DEFAULT_AGENCY_ID)))
        if session.info.get("agency_id") != agency_id and session.in_transaction():
            await session.rollback()
        await set_agency_context(session, agency_id)
        receipt = (
            await session.execute(
                insert(ReplayReceipt)
                .values(id=uuid.UUID(identity))
                .on_conflict_do_nothing()
                .returning(ReplayReceipt.id)
            )
        ).scalar_one_or_none()
        if receipt is not None:
            audit = AuditEventInput(
                client_id=uuid.UUID(payload["client_id"]),
                type="provider_call",
                provider=payload["provider"],
                model=payload["model"],
                cost_micro=payload["cost_micro"],
                estimated=True,
            )
            if payload.get("held"):
                held = payload["held"]
                await settle(
                    session,
                    HeldReservation(
                        uuid.UUID(held["id"]),
                        uuid.UUID(held["budget_id"]),
                        uuid.UUID(held["run_id"]),
                        held["est_micro"],
                    ),
                    payload["cost_micro"],
                    audit,
                )
            else:
                workflow_id = uuid.UUID(payload["workflow_id"])
                budget = (
                    await session.execute(
                        select(Budget)
                        .where(
                            Budget.workflow_id == workflow_id, Budget.period == payload["period"]
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                budget.spent_micro += payload["cost_micro"]
                statement = insert(Run).values(
                    workflow_id=workflow_id,
                    external_run_id=payload["run_key"],
                    cost_micro=payload["cost_micro"],
                    step_count=1,
                )
                run_id = (
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[Run.workflow_id, Run.external_run_id],
                            set_={
                                "cost_micro": Run.cost_micro + payload["cost_micro"],
                                "step_count": Run.step_count + 1,
                            },
                        ).returning(Run.id)
                    )
                ).scalar_one()
                await append_event(session, audit, run_id)
                if budget.spent_micro + budget.reserved_micro > budget.cap_micro:
                    from policy.loop import trip

                    await trip(session, workflow_id, "budget_overrun", run_id=run_id)
                await session.commit()
        else:
            await session.rollback()

        def remove(queue_id: str = identity) -> None:
            with connect() as conn:
                conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))

        await asyncio.to_thread(remove)
        done += 1
    return done


async def main() -> None:
    from core.database import session_factory

    async with session_factory() as session:
        print(f"Replayed {await replay(session)} accounting entries")


if __name__ == "__main__":
    asyncio.run(main())
