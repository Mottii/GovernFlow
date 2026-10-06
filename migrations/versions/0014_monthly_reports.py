"""Human supplied hours saved, stored as integer minutes by UTC month."""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE monthly_metrics (
      client_id uuid NOT NULL REFERENCES clients(id), period text NOT NULL,
      minutes_saved bigint NOT NULL CHECK (minutes_saved >= 0),
      basis text NOT NULL CHECK (basis IN ('measured','estimate')), note text NOT NULL DEFAULT '',
      updated_by text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (client_id, period))""")


def downgrade() -> None:
    op.drop_table("monthly_metrics")
