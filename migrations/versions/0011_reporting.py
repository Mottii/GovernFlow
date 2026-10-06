"""Dedicated provider accounts and daily reconciliation snapshots."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE provider_accounts (
      client_id uuid NOT NULL REFERENCES clients(id), provider text NOT NULL,
      external_id text NOT NULL, credential_label text NOT NULL DEFAULT 'default',
      inference_key_id text, validated_at timestamptz,
      PRIMARY KEY (client_id, provider), UNIQUE (provider, external_id),
      CHECK (provider IN ('openai','anthropic')))""")
    op.execute("""CREATE TABLE reconcile_snapshots (
      client_id uuid NOT NULL REFERENCES clients(id), provider text NOT NULL, day date NOT NULL,
      kind text NOT NULL, dimension text NOT NULL,
      gateway jsonb NOT NULL, observed jsonb NOT NULL, source_groups jsonb NOT NULL DEFAULT '[]',
      fetched_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (client_id, provider, day, kind, dimension))""")
    op.execute("""CREATE TABLE reconcile_jobs (
      client_id uuid NOT NULL REFERENCES clients(id), provider text NOT NULL, day date NOT NULL,
      status text NOT NULL, error text, fetched_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (client_id, provider, day))""")


def downgrade() -> None:
    for table in ("reconcile_jobs", "reconcile_snapshots", "provider_accounts"):
        op.drop_table(table)
