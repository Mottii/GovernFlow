"""Replay protection for signed integration decisions."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE webhook_receipts (id uuid PRIMARY KEY, "
        "received_at timestamptz NOT NULL DEFAULT now())"
    )


def downgrade() -> None:
    op.drop_table("webhook_receipts")
