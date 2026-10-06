"""Durable governance alerts."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE alerts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      client_id uuid NOT NULL REFERENCES clients(id),
      workflow_id uuid REFERENCES workflows(id),
      kind text NOT NULL,
      details jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    op.drop_table("alerts")
