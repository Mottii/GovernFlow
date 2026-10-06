"""Encrypted action content for approval review."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE approvals ADD COLUMN content_ciphertext bytea")
    op.execute("ALTER TABLE approvals ADD COLUMN content_nonce bytea")
    op.execute("ALTER TABLE approvals ADD COLUMN timeout_outcome text NOT NULL DEFAULT 'deny'")
    op.execute(
        "ALTER TABLE approvals ADD CONSTRAINT approval_timeout_outcome "
        "CHECK (timeout_outcome IN ('allow','deny'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE approvals DROP CONSTRAINT approval_timeout_outcome")
    op.execute("ALTER TABLE approvals DROP COLUMN timeout_outcome")
    op.execute("ALTER TABLE approvals DROP COLUMN content_nonce")
    op.execute("ALTER TABLE approvals DROP COLUMN content_ciphertext")
