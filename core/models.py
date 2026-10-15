import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


DEFAULT_AGENCY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Agency(Base):
    __tablename__ = "agencies"
    __table_args__ = (CheckConstraint("status IN ('active','suspended')"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("agency_id", "id"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=str(DEFAULT_AGENCY_ID),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="180")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Workflow(Base):
    __tablename__ = "workflows"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        UniqueConstraint("client_id", "name"),
        CheckConstraint("risk_class IN ('R0','R1','R2','R3')"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    risk_class: Mapped[str] = mapped_column(Text, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")


class VirtualKey(Base):
    __tablename__ = "virtual_keys"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        UniqueConstraint("client_id", "provider", "label"),
        CheckConstraint("provider IN ('openai','anthropic','gemini')"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class Budget(Base):
    __tablename__ = "budgets"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        UniqueConstraint("workflow_id", "period"),
        CheckConstraint("spent_micro >= 0 AND reserved_micro >= 0"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    period: Mapped[str] = mapped_column(Text, nullable=False)
    cap_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spent_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reserved_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class Run(Base):
    __tablename__ = "runs"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        Index("runs_workflow_external_idx", "workflow_id", "external_run_id", unique=True),
        CheckConstraint("reserved_micro >= 0", name="runs_reserved_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    external_run_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reserved_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cap_micro: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="9223372036854775807"
    )


class Reservation(Base):
    __tablename__ = "reservations"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        CheckConstraint("est_micro > 0"),
        CheckConstraint("status IN ('reserved','settled','released')"),
        Index("reservations_status_created_idx", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    budget_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    est_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_micro: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="reserved")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (Index("events_client_id_idx", "client_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cost_micro: Mapped[int | None] = mapped_column(BigInteger)
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    content_ref: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class KillFlag(Base):
    __tablename__ = "kill_flags"
    __table_args__ = (CheckConstraint("scope IN ('global','client','workflow')"),)

    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agencies.id")
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_id: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint(
            "input >= 0 AND output >= 0 AND cache_read >= 0 AND cache_write >= 0 "
            "AND cache_write_hour >= 0 AND reasoning >= 0",
            name="prices_nonnegative",
        ),
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    cache_write_hour: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    input: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cache_write: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reasoning: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class Approval(Base):
    __tablename__ = "approvals"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','denied','expired')"),
        CheckConstraint("timeout_outcome IN ('allow','deny')", name="approval_timeout_outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    timeout_outcome: Mapped[str] = mapped_column(Text, nullable=False, server_default="deny")
    action_name: Mapped[str] = mapped_column(Text, nullable=False)
    action_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    action_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    approver: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (CheckConstraint("count > 0"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), primary_key=True
    )
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    dedup_key: Mapped[str | None] = mapped_column(Text, unique=True)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clients.id"))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReplayReceipt(Base):
    __tablename__ = "replay_receipts"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class Notification(Base):
    __tablename__ = "notifications"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        UniqueConstraint("approval_id", "destination_id"),
        ForeignKeyConstraint(
            ["agency_id", "destination_id"],
            ["integration_destinations.agency_id", "integration_destinations.id"],
        ),
    )
    destination_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("approvals.id"))
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProviderAccount(Base):
    __tablename__ = "provider_accounts"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        CheckConstraint("provider IN ('openai','anthropic')"),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    credential_label: Mapped[str] = mapped_column(Text, server_default="default")
    inference_key_id: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReconcileSnapshot(Base):
    __tablename__ = "reconcile_snapshots"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    dimension: Mapped[str] = mapped_column(Text, primary_key=True)
    gateway: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_groups: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReconcileJob(Base):
    __tablename__ = "reconcile_jobs"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin','operator','approver','viewer')"),)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    password_salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=str(DEFAULT_AGENCY_ID),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginBucket(Base):
    __tablename__ = "login_buckets"
    identity: Mapped[str] = mapped_column(Text, primary_key=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


class MonthlyMetric(Base):
    __tablename__ = "monthly_metrics"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agencies.id"),
        nullable=False,
        server_default=text("NULLIF(current_setting('governflow.agency_id', true), '')::uuid"),
    )

    __table_args__ = (
        CheckConstraint("minutes_saved >= 0"),
        CheckConstraint("basis IN ('measured','estimate')"),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), primary_key=True
    )
    period: Mapped[str] = mapped_column(Text, primary_key=True)
    minutes_saved: Mapped[int] = mapped_column(BigInteger, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgencyMembership(Base):
    __tablename__ = "agency_memberships"
    __table_args__ = (CheckConstraint("role IN ('admin','operator','approver','viewer')"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agencies.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    all_clients: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class ClientAssignment(Base):
    __tablename__ = "client_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "user_id"],
            ["agency_memberships.agency_id", "agency_memberships.user_id"],
        ),
        ForeignKeyConstraint(["agency_id", "client_id"], ["clients.agency_id", "clients.id"]),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class ExternalLoginIdentity(Base):
    __tablename__ = "external_login_identities"
    __table_args__ = (
        Index(
            "telegram_one_account",
            "user_id",
            unique=True,
            postgresql_where=text("issuer='telegram'"),
        ),
    )

    issuer: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class ReviewerGroup(Base):
    __tablename__ = "reviewer_groups"
    __table_args__ = (
        UniqueConstraint("agency_id", "id"),
        UniqueConstraint("agency_id", "client_id", "id"),
        UniqueConstraint("client_id", "name"),
        ForeignKeyConstraint(["agency_id", "client_id"], ["clients.agency_id", "clients.id"]),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agencies.id"))
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class ReviewerGroupMember(Base):
    __tablename__ = "reviewer_group_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "group_id"], ["reviewer_groups.agency_id", "reviewer_groups.id"]
        ),
        ForeignKeyConstraint(
            ["agency_id", "user_id"], ["agency_memberships.agency_id", "agency_memberships.user_id"]
        ),
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class ReviewerAssignment(Base):
    __tablename__ = "reviewer_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "client_id", "group_id"],
            ["reviewer_groups.agency_id", "reviewer_groups.client_id", "reviewer_groups.id"],
        ),
        ForeignKeyConstraint(
            ["agency_id", "client_id", "workflow_id"],
            ["workflows.agency_id", "workflows.client_id", "workflows.id"],
        ),
        Index(
            "reviewer_client_scope",
            "group_id",
            unique=True,
            postgresql_where=text("workflow_id IS NULL"),
        ),
        Index(
            "reviewer_workflow_scope",
            "group_id",
            "workflow_id",
            unique=True,
            postgresql_where=text("workflow_id IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class IntegrationDestination(Base):
    __tablename__ = "integration_destinations"
    __table_args__ = (
        UniqueConstraint("agency_id", "id"),
        UniqueConstraint("agency_id", "legacy_source"),
        ForeignKeyConstraint(["agency_id", "client_id"], ["clients.agency_id", "clients.id"]),
        ForeignKeyConstraint(
            ["agency_id", "client_id", "workflow_id"],
            ["workflows.agency_id", "workflows.client_id", "workflows.id"],
        ),
        ForeignKeyConstraint(
            ["agency_id", "client_id", "group_id"],
            ["reviewer_groups.agency_id", "reviewer_groups.client_id", "reviewer_groups.id"],
        ),
        CheckConstraint("channel IN ('telegram','email','webhook')"),
        CheckConstraint("(workflow_id IS NULL AND group_id IS NULL) OR client_id IS NOT NULL"),
        CheckConstraint(
            "NOT enabled OR (client_id IS NOT NULL AND "
            "((channel IN ('email','telegram') AND group_id IS NOT NULL) OR "
            "(channel='webhook' AND credential_hash IS NOT NULL AND "
            "secret_ciphertext IS NOT NULL AND secret_nonce IS NOT NULL AND "
            "service_identity IS NOT NULL)))"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agencies.id"))
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    legacy_source: Mapped[str | None] = mapped_column(Text)
    service_identity: Mapped[str | None] = mapped_column(Text)
    credential_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)


class TelegramLinkChallenge(Base):
    __tablename__ = "telegram_link_challenges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "user_id"], ["agency_memberships.agency_id", "agency_memberships.user_id"]
        ),
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgencyPrice(Base):
    __tablename__ = "agency_prices"
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agencies.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    rates: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_by: Mapped[str] = mapped_column(Text)


# Composite ownership constraints supplement the existing stable primary keys.
from core.ownership import DERIVE_FUNCTION, PARENTS, SECONDARY, trigger_sql  # noqa: E402

for _name in ("workflows", "budgets", "runs", "approvals"):
    Base.metadata.tables[_name].append_constraint(UniqueConstraint("agency_id", "id"))
for _name, (_parent, _field) in PARENTS.items():
    _table = Base.metadata.tables[_name]
    for _target, _column in [(_parent, _field), *SECONDARY.get(_name, [])]:
        _table.append_constraint(
            ForeignKeyConstraint(
                ["agency_id", _column],
                [f"{_target}.agency_id", f"{_target}.id"],
                name=f"{_name}_{_column}_agency_fk",
            )
        )
# Alembic installs a frozen copy of this DDL; create_all is for disposable test databases.
event.listen(
    Base.metadata,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        DERIVE_FUNCTION.replace("%", "%%")
    ),
)
for _name, (_parent, _field) in PARENTS.items():
    event.listen(
        Base.metadata,
        "after_create",
        DDL(  # type: ignore[no-untyped-call]
            trigger_sql(_name, _parent, _field)
        ),
    )

for _parent, _field in (("clients", "client_id"), ("workflows", "workflow_id")):
    Base.metadata.tables["kill_flags"].append_constraint(
        ForeignKeyConstraint(["agency_id", _field], [f"{_parent}.agency_id", f"{_parent}.id"])
    )
event.listen(
    Base.metadata,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        """CREATE OR REPLACE FUNCTION governflow_kill_owner() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog, public AS $$ BEGIN
        IF NEW.scope='client' THEN
            NEW.client_id := NEW.scope_id::uuid;
            SELECT agency_id INTO NEW.agency_id FROM public.clients WHERE id=NEW.client_id;
        ELSIF NEW.scope='workflow' THEN
            NEW.workflow_id := NEW.scope_id::uuid;
            SELECT agency_id INTO NEW.agency_id FROM public.workflows WHERE id=NEW.workflow_id;
        END IF;
        RETURN NEW; END $$"""
    ),
)
event.listen(
    Base.metadata,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER kill_owner BEFORE INSERT OR UPDATE ON kill_flags "
        "FOR EACH ROW EXECUTE FUNCTION governflow_kill_owner()"
    ),
)

Base.metadata.tables["workflows"].append_constraint(
    UniqueConstraint("agency_id", "client_id", "id")
)
