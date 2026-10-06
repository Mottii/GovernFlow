"""Per-run reservations and idempotent settlement.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN reserved_micro bigint NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE runs ADD COLUMN cap_micro bigint NOT NULL DEFAULT 9223372036854775807")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_reserved_nonnegative CHECK (reserved_micro >= 0)"
    )
    op.execute(
        "CREATE UNIQUE INDEX runs_workflow_external_idx ON runs (workflow_id, external_run_id)"
    )
    op.execute("""CREATE TABLE reservations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      budget_id uuid NOT NULL REFERENCES budgets(id),
      run_id uuid NOT NULL REFERENCES runs(id),
      est_micro bigint NOT NULL CHECK (est_micro > 0),
      actual_micro bigint,
      status text NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved','settled','released')),
      created_at timestamptz NOT NULL DEFAULT now(),
      settled_at timestamptz)""")
    op.execute("CREATE INDEX reservations_status_created_idx ON reservations (status, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE reservations")
    op.execute("DROP INDEX runs_workflow_external_idx")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_reserved_nonnegative")
    op.execute("ALTER TABLE runs DROP COLUMN cap_micro")
    op.execute("ALTER TABLE runs DROP COLUMN reserved_micro")
