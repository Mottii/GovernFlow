import os
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import KillFlag
from gateway.auth import AuthContext
from gateway.errors import GatewayError


@dataclass(frozen=True)
class CachedFlag:
    expires_at: float
    active: bool
    reason: str | None


_cache: dict[tuple[str, str], CachedFlag] = {}


async def check_kill(session: AsyncSession, auth: AuthContext) -> None:
    ttl = min(5.0, max(0.0, float(os.environ.get("KILL_CACHE_TTL_S", "5"))))
    scopes = (
        ("global", ""),
        ("client", str(auth.client_id)),
        ("workflow", str(auth.workflow_id)),
    )
    for scope, scope_id in scopes:
        cache_key = (scope, scope_id)
        cached = _cache.get(cache_key)
        if cached is None or cached.expires_at <= time.monotonic():
            row = (
                await session.execute(
                    select(KillFlag).where(KillFlag.scope == scope, KillFlag.scope_id == scope_id)
                )
            ).scalar_one_or_none()
            cached = CachedFlag(
                time.monotonic() + ttl, row.active if row else False, row.reason if row else None
            )
            _cache[cache_key] = cached
        if cached.active:
            raise GatewayError(503, "workflow_killed", cached.reason or "Automation is disabled")


def clear_cache() -> None:
    _cache.clear()
