"""Role and tenant scoped users with revocable hashed login sessions."""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE users (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL UNIQUE,
      password_hash bytea NOT NULL, password_salt bytea NOT NULL,
      role text NOT NULL CHECK (role IN ('admin','operator','approver','viewer')),
      client_id uuid REFERENCES clients(id), active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE admin_sessions (
      token_hash bytea PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
      expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE login_buckets (
      identity text NOT NULL, minute timestamptz NOT NULL, count int NOT NULL,
      PRIMARY KEY (identity, minute))""")


def downgrade() -> None:
    for table in ("login_buckets", "admin_sessions", "users"):
        op.drop_table(table)
