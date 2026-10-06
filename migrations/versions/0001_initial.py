"""Initial schema from section 18.4 of the blueprint.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("""CREATE TABLE clients (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name text NOT NULL, status text NOT NULL DEFAULT 'active',
      retention_days int NOT NULL DEFAULT 180, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE workflows (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      client_id uuid NOT NULL REFERENCES clients(id), name text NOT NULL,
      risk_class text NOT NULL CHECK (risk_class IN ('R0','R1','R2','R3')),
      policy jsonb NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'active',
      UNIQUE (client_id, name))""")
    op.execute("""CREATE TABLE virtual_keys (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workflow_id uuid NOT NULL REFERENCES workflows(id),
      key_hash bytea NOT NULL UNIQUE, prefix text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz)""")
    op.execute("""CREATE TABLE provider_credentials (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      client_id uuid NOT NULL REFERENCES clients(id),
      provider text NOT NULL CHECK (provider IN ('openai','anthropic','gemini')),
      label text NOT NULL DEFAULT 'default', ciphertext bytea NOT NULL, nonce bytea NOT NULL,
      UNIQUE (client_id, provider, label))""")
    op.execute("""CREATE TABLE budgets (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workflow_id uuid NOT NULL REFERENCES workflows(id),
      period text NOT NULL, cap_micro bigint NOT NULL,
      spent_micro bigint NOT NULL DEFAULT 0,
      reserved_micro bigint NOT NULL DEFAULT 0,
      UNIQUE (workflow_id, period),
      CHECK (spent_micro >= 0 AND reserved_micro >= 0))""")
    op.execute("""CREATE TABLE runs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workflow_id uuid NOT NULL REFERENCES workflows(id), external_run_id text,
      started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
      status text NOT NULL DEFAULT 'running', step_count int NOT NULL DEFAULT 0,
      cost_micro bigint NOT NULL DEFAULT 0)""")
    op.execute("""CREATE TABLE events (
      id bigserial PRIMARY KEY, client_id uuid NOT NULL, run_id uuid,
      ts timestamptz NOT NULL DEFAULT now(), type text NOT NULL,
      provider text, model text, usage jsonb, cost_micro bigint,
      estimated boolean NOT NULL DEFAULT false, content_ref text,
      prev_hash bytea, hash bytea NOT NULL)""")
    op.execute("CREATE INDEX events_client_id_idx ON events (client_id, id)")
    op.execute("""CREATE FUNCTION events_immutable() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'events is append-only'; END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER events_no_change BEFORE UPDATE OR DELETE ON events
      FOR EACH ROW EXECUTE FUNCTION events_immutable()""")
    op.execute("""CREATE TABLE kill_flags (
      scope text NOT NULL CHECK (scope IN ('global','client','workflow')),
      scope_id text NOT NULL DEFAULT '', active boolean NOT NULL, reason text,
      set_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (scope, scope_id))""")
    op.execute("""CREATE TABLE prices (
      provider text NOT NULL, model text NOT NULL, effective_from date NOT NULL,
      input bigint NOT NULL, output bigint NOT NULL,
      cache_read bigint NOT NULL DEFAULT 0, cache_write bigint NOT NULL DEFAULT 0,
      reasoning bigint NOT NULL DEFAULT 0,
      PRIMARY KEY (provider, model, effective_from))""")
    op.execute("""CREATE TABLE approvals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workflow_id uuid NOT NULL REFERENCES workflows(id), run_id uuid,
      action_name text NOT NULL, action_hash bytea NOT NULL, action_summary text NOT NULL,
      status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','denied','expired')),
      approver text, decided_at timestamptz, expires_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    for table in (
        "approvals",
        "prices",
        "kill_flags",
        "events",
        "runs",
        "budgets",
        "provider_credentials",
        "virtual_keys",
        "workflows",
        "clients",
    ):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION events_immutable")
