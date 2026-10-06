import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Price


def catalog() -> dict[str, Any]:
    return dict(json.loads(Path(__file__).with_name("model_prices.json").read_text()))


async def seed(session: AsyncSession) -> int:
    data = catalog()
    for item in data["prices"]:
        await session.execute(
            insert(Price)
            .values(
                **item,
                effective_from=date(2026, 10, 6),
                verified_at=datetime.fromisoformat(data["verified_at"]),
            )
            .on_conflict_do_nothing()
        )
    await session.commit()
    return len(data["prices"])


async def main() -> None:
    from core.database import session_factory

    async with session_factory() as session:
        print(f"Seeded {await seed(session)} verified model prices")


if __name__ == "__main__":
    asyncio.run(main())
