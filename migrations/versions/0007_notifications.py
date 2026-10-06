"""Durable approval notification outbox."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE notifications (
      id uuid PRIMARY KEY, approval_id uuid NOT NULL REFERENCES approvals(id),
      channel text NOT NULL, status text NOT NULL DEFAULT 'pending',
      attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now(),
      UNIQUE (approval_id, channel))""")


def downgrade() -> None:
    op.drop_table("notifications")
