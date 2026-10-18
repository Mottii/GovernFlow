import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from audit.chain import AuditEventInput
from core.models import AgencyPrice, Budget, Price, Reservation, Run
from core.usage import Usage
from policy.loop import trip


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def estimate_micro(request_body: bytes, max_output: int, price: Price) -> int:
    # Use the full UTF-8 body length as a conservative text estimate; never discount
    # cached input until the provider supplies measured usage.
    estimated_input = len(request_body)
    base_input = max(price.input, price.cache_write, price.cache_write_hour)
    return max(
        1,
        bill(
            estimated_input * base_input,
            max_output * max(price.output, price.reasoning),
            estimated_input,
            price,
        ),
    )


def bill(input_charge: int, output_charge: int, input_tokens: int, price: Price) -> int:
    tier = (price.meter or {}).get("long_context")
    if tier and input_tokens > tier["threshold_tokens"]:
        charge = input_charge * Fraction(*tier["input_multiplier"]) + output_charge * Fraction(
            *tier["output_multiplier"]
        )
        return ceil_div(charge.numerator, charge.denominator * 1_000_000)
    return ceil_div(input_charge + output_charge, 1_000_000)


def cost_micro(usage: Usage, price: Price) -> int:
    return bill(
        usage.input * price.input
        + usage.cache_read * price.cache_read
        + usage.cache_write * price.cache_write
        + usage.cache_write_hour * price.cache_write_hour,
        usage.output * price.output + usage.reasoning * price.reasoning,
        usage.input + usage.cache_read + usage.cache_write + usage.cache_write_hour,
        price,
    )


async def current_price(session: AsyncSession, provider: str, model: str) -> Price | None:
    agency_id = session.info.get("agency_id")
    if agency_id:
        override = (
            await session.execute(
                select(AgencyPrice)
                .where(
                    AgencyPrice.agency_id == agency_id,
                    AgencyPrice.provider == provider,
                    AgencyPrice.model == model,
                    AgencyPrice.effective_from <= datetime.now(UTC).date(),
                )
                .order_by(AgencyPrice.effective_from.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if override:
            return Price(
                provider=provider,
                model=model,
                effective_from=override.effective_from,
                **override.rates,
            )
    return (
        await session.execute(
            select(Price)
            .where(
                Price.provider == provider,
                Price.model == model,
                Price.effective_from <= datetime.now(UTC).date(),
            )
            .order_by(Price.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class HeldReservation:
    id: uuid.UUID
    budget_id: uuid.UUID
    run_id: uuid.UUID
    est_micro: int


async def reserve(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    run_key: str,
    est_micro: int,
    per_run_cap_micro: int,
    max_steps: int | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    pricing: dict[str, Any] | None = None,
) -> HeldReservation | None:
    from policy.accounting import database_now

    period = (await database_now(session)).strftime("%Y-%m")
    # Settlement takes the budget row before the run row. Reserve in that
    # order too, otherwise concurrent calls can deadlock with settlements.
    budget_id = (
        await session.execute(
            update(Budget)
            .where(
                Budget.workflow_id == workflow_id,
                Budget.period == period,
                Budget.spent_micro + Budget.reserved_micro + est_micro <= Budget.cap_micro,
            )
            .values(reserved_micro=Budget.reserved_micro + est_micro)
            .returning(Budget.id)
        )
    ).scalar_one_or_none()
    if budget_id is None:
        await session.rollback()
        return None
    await session.execute(
        insert(Run)
        .values(
            workflow_id=workflow_id,
            external_run_id=run_key,
            cap_micro=per_run_cap_micro,
        )
        .on_conflict_do_nothing(index_elements=[Run.workflow_id, Run.external_run_id])
    )
    run_id = (
        await session.execute(
            select(Run.id).where(Run.workflow_id == workflow_id, Run.external_run_id == run_key)
        )
    ).scalar_one()
    accepted_run = (
        await session.execute(
            update(Run)
            .where(
                Run.id == run_id,
                Run.cost_micro + Run.reserved_micro + est_micro <= Run.cap_micro,
                Run.cost_micro + Run.reserved_micro + est_micro <= per_run_cap_micro,
                Run.step_count < (max_steps if max_steps is not None else 2**31 - 1),
            )
            .values(
                reserved_micro=Run.reserved_micro + est_micro,
                step_count=Run.step_count + 1,
            )
            .returning(Run.id)
        )
    ).scalar_one_or_none()
    if accepted_run is None:
        await session.rollback()
        persisted_run = await session.get(Run, run_id)
        await trip(session, workflow_id, "loop_breaker", run_id=run_id if persisted_run else None)
        await session.commit()
        return None
    record = Reservation(budget_id=budget_id, run_id=run_id, est_micro=est_micro)
    session.add(record)
    await session.flush()
    from policy.accounting import register

    await register(session, record, workflow_id, provider=provider, model=model, pricing=pricing)
    await session.commit()
    return HeldReservation(record.id, budget_id, run_id, est_micro)


async def settle(
    session: AsyncSession,
    held: HeldReservation,
    actual_micro: int,
    audit: AuditEventInput | None = None,
) -> bool:
    from policy.accounting import Evidence, apply, locked_call

    call = await locked_call(session, held.id)
    if audit:
        call.provider, call.model = audit.provider or call.provider, audit.model or call.model
    kind: Literal["estimate", "measured"] = "estimate" if audit and audit.estimated else "measured"
    evidence = Evidence(
        evidence_id=uuid.uuid5(held.id, f"{kind}:{actual_micro}"),
        agency_id=call.agency_id,
        client_id=call.client_id,
        call_id=call.id,
        kind=kind,
        amount_micro=actual_micro,
        reason="missing_usage" if kind == "estimate" else "terminal_usage",
        usage=audit.usage if audit else None,
    )
    result = await apply(session, evidence)
    await session.commit()
    return result == "applied"


async def release(
    session: AsyncSession,
    held: HeldReservation,
    audit: AuditEventInput | None = None,
) -> bool:
    from policy.accounting import Evidence, apply, locked_call

    call = await locked_call(session, held.id)
    evidence = Evidence(
        evidence_id=uuid.uuid5(held.id, "void"),
        agency_id=call.agency_id,
        client_id=call.client_id,
        call_id=call.id,
        kind="void",
        amount_micro=0,
        reason=audit.type if audit else "not_dispatched",
    )
    result = await apply(session, evidence)
    await session.commit()
    return result == "applied"
