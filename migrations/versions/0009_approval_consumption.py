"""Atomic single-use execution approvals."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE approvals ADD COLUMN consumed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE approvals DROP COLUMN consumed_at")
