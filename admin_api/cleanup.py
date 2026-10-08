import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from core.database import agency_sessions
from core.models import AdminSession, LoginBucket


async def main() -> None:
    async for session in agency_sessions():
        now = datetime.now(UTC)
        await session.execute(delete(AdminSession).where(AdminSession.expires_at < now))
        await session.execute(
            delete(LoginBucket).where(LoginBucket.minute < now - timedelta(days=1))
        )
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
