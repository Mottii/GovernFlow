from datetime import UTC, datetime
from fnmatch import fnmatchcase

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import RateBucket
from gateway.auth import AuthContext
from gateway.errors import GatewayError
from policy.loop import loop_policy


def allowed_models(auth: AuthContext) -> list[str]:
    models = auth.policy.get("models", [])
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
        raise GatewayError(503, "invalid_policy", "Model allowlist is invalid")
    return models


async def check_traffic(session: AsyncSession, auth: AuthContext, model: str) -> None:
    if not any(fnmatchcase(model, pattern) for pattern in allowed_models(auth)):
        raise GatewayError(403, "model_not_allowed", "Model is not allowed for this workflow")
    limit = auth.policy.get("rpm", 60)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000000:
        raise GatewayError(503, "invalid_policy", "RPM limit is invalid")
    _, _, loop_rpm = loop_policy(auth.policy)
    now = datetime.now(UTC)
    minute = now.replace(second=0, microsecond=0)
    statement = insert(RateBucket).values(workflow_id=auth.workflow_id, minute=minute, count=1)
    accepted = (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[RateBucket.workflow_id, RateBucket.minute],
                set_={"count": RateBucket.count + 1},
                where=RateBucket.count < min(limit, loop_rpm),
            ).returning(RateBucket.count)
        )
    ).scalar_one_or_none()
    # The counter is shared by replicas and persists even if a later call fails.
    await session.commit()
    if accepted is None:
        raise GatewayError(
            429, "rate_limit_exceeded", "Workflow rate limit exceeded", retry_after=60 - now.second
        )
