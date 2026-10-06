"""Deduplicate reconciliation alerts across daily refreshes."""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alerts ADD COLUMN dedup_key text UNIQUE")


def downgrade() -> None:
    op.drop_column("alerts", "dedup_key")
