import os
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    )


def make_engine(url: str | None = None) -> AsyncEngine:
    result = create_async_engine(url or database_url(), pool_pre_ping=True)

    @event.listens_for(result.sync_engine, "connect")
    def restricted_login(connection: object, record: object) -> None:
        if not agency_rls_enabled() or os.environ.get("MAINTENANCE_MODE", "").lower() == "true":
            return
        cursor = connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("""SELECT r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
                OR r.rolcreatedb OR r.rolreplication
                OR NOT pg_has_role(current_user,'governflow_runtime','USAGE')
                OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND pg_has_role(current_user,c.relowner,'USAGE'))
                OR EXISTS (SELECT 1 FROM pg_database d WHERE d.datname=current_database()
                    AND pg_has_role(current_user,d.datdba,'USAGE'))
                FROM pg_roles r WHERE r.rolname=current_user""")
            if cursor.fetchone()[0]:
                raise RuntimeError("Runtime DATABASE_URL must use a restricted non-owner login")
        finally:
            cursor.close()

    return result


engine = make_engine()
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


def agency_rls_enabled() -> bool:
    # Only metadata-created test databases may disable the migration's auth functions.
    return os.environ.get("ENFORCE_AGENCY_RLS", "true").lower() == "true"


@event.listens_for(Session, "after_begin")
def restore_agency_context(session: Session, transaction: object, connection: Connection) -> None:
    agency_id = session.info.get("agency_id")
    connection.execute(
        text("SELECT set_config('governflow.agency_id', :agency, true)"),
        {"agency": str(agency_id) if agency_id else ""},
    )


async def set_agency_context(session: AsyncSession, agency_id: uuid.UUID) -> None:
    existing = session.info.get("agency_id")
    if existing is not None and existing != agency_id and session.in_transaction():
        raise RuntimeError("Cannot change agency inside an active transaction")
    if existing is not None and existing != agency_id:
        session.expunge_all()
    session.info["agency_id"] = agency_id
    await session.execute(
        text("SELECT set_config('governflow.agency_id', :agency, true)"), {"agency": str(agency_id)}
    )


async def agency_sessions() -> AsyncIterator[AsyncSession]:
    from core.models import Agency

    # The worker enumerates agencies, then every job transaction uses exactly one context.
    async with session_factory() as directory:
        agencies = list(
            (await directory.execute(select(Agency.id).where(Agency.status == "active"))).scalars()
        )
    for agency_id in agencies:
        async with session_factory() as session:
            await set_agency_context(session, agency_id)
            yield session
