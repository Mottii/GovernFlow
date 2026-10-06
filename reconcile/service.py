import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Event, ProviderAccount, ReconcileJob, ReconcileSnapshot
from gateway.adapters.anthropic import message_usage
from gateway.adapters.gemini import content_usage
from gateway.adapters.openai import chat_usage
from gateway.adapters.openai_more import embeddings_usage, responses_usage
from reconcile.providers import add, fetch


async def gateway_totals(
    session: AsyncSession, client_id: uuid.UUID, provider: str, day: date
) -> tuple[dict[str, dict[str, int]], int, int]:
    start = datetime.combine(day, time(), UTC)
    events = (
        (
            await session.execute(
                select(Event).where(
                    Event.client_id == client_id,
                    Event.provider == provider,
                    Event.type == "provider_call",
                    Event.ts >= start,
                    Event.ts < start + timedelta(days=1),
                )
            )
        )
        .scalars()
        .all()
    )
    models: dict[str, dict[str, int]] = {}
    cost = estimated = 0
    for event in events:
        cost += event.cost_micro or 0
        estimated += int(event.estimated)
        if event.usage is None or event.model is None:
            continue
        body = {"usage": event.usage}
        if provider == "anthropic":
            usage = message_usage(body)
        elif provider == "gemini":
            usage = content_usage({"usageMetadata": event.usage})
        else:
            usage = chat_usage(body) or responses_usage(body) or embeddings_usage(body)
        if usage:
            add(
                models,
                event.model,
                usage.input + usage.cache_read + usage.cache_write + usage.cache_write_hour,
                usage.output + usage.reasoning,
                usage.cache_read,
                usage.cache_write + usage.cache_write_hour,
            )
    return models, cost, estimated


async def reconcile(session: AsyncSession, account: ProviderAccount, day: date) -> bool:
    key = {"client_id": account.client_id, "provider": account.provider, "day": day}
    now = datetime.now(UTC)
    try:
        observed = await fetch(account, day)
        models, cost, estimated = await gateway_totals(
            session, account.client_id, account.provider, day
        )
        await session.execute(
            delete(ReconcileSnapshot).where(
                ReconcileSnapshot.client_id == account.client_id,
                ReconcileSnapshot.provider == account.provider,
                ReconcileSnapshot.day == day,
            )
        )
        zero = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        for model in set(models) | set(observed.usage):
            session.add(
                ReconcileSnapshot(
                    **key,
                    kind="usage",
                    dimension=model,
                    gateway=models.get(model, zero),
                    observed=observed.usage.get(model, zero),
                    fetched_at=now,
                )
            )
        session.add(
            ReconcileSnapshot(
                **key,
                kind="cost",
                dimension="account_total",
                gateway={"cost_micro": cost, "estimated_calls": estimated},
                observed={"cost_micro": observed.cost_micro},
                source_groups=observed.cost_groups,
                fetched_at=now,
            )
        )
        status, error = "complete", None
    except (httpx.HTTPError, ValueError, KeyError, RuntimeError):
        await session.rollback()
        status, error = "failed", "Provider reporting failed or is not configured"
    statement = insert(ReconcileJob).values(**key, status=status, error=error, fetched_at=now)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ReconcileJob.client_id, ReconcileJob.provider, ReconcileJob.day],
            set_={"status": status, "error": error, "fetched_at": now},
        )
    )
    await session.commit()
    return status == "complete"


async def run(day: date | None = None) -> dict[str, int]:
    from core.database import session_factory

    async with session_factory() as session:
        keys = (
            await session.execute(select(ProviderAccount.client_id, ProviderAccount.provider))
        ).all()
    dates = (
        [day]
        if day
        else [datetime.now(UTC).date() - timedelta(days=offset) for offset in (1, 2, 3)]
    )
    completed = failed = 0
    for client_id, provider in keys:
        for target in dates:
            async with session_factory() as session:
                account = await session.get(ProviderAccount, (client_id, provider))
                if account is None:
                    continue
                if await reconcile(session, account, target):
                    completed += 1
                else:
                    failed += 1
    return {"complete": completed, "failed": failed}


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args()
    print(await run(args.day))


if __name__ == "__main__":
    asyncio.run(main())
