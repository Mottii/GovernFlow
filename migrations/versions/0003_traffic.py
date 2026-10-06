"""Shared rate limits."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_buckets",
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id"),
            primary_key=True,
        ),
        sa.Column("minute", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count > 0"),
    )


def downgrade() -> None:
    op.drop_table("rate_buckets")
