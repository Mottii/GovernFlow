"""Idempotent replay receipts."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE replay_receipts (id uuid PRIMARY KEY)")


def downgrade() -> None:
    op.drop_table("replay_receipts")
