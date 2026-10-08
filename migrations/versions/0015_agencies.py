"""Backfill agency membership and explicit client grants without changing identities."""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

DEFAULT_AGENCY = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agencies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(f"INSERT INTO agencies (id, name) VALUES ('{DEFAULT_AGENCY}', 'Default agency')")
    # Expand and backfill before enforcing ownership; preserve all existing primary keys.
    op.execute("ALTER TABLE clients ADD COLUMN agency_id uuid")
    op.execute(f"UPDATE clients SET agency_id = '{DEFAULT_AGENCY}'")
    op.execute(f"ALTER TABLE clients ALTER COLUMN agency_id SET DEFAULT '{DEFAULT_AGENCY}'")
    op.execute("ALTER TABLE clients ALTER COLUMN agency_id SET NOT NULL")
    op.execute("ALTER TABLE clients ADD FOREIGN KEY (agency_id) REFERENCES agencies(id)")
    op.execute("ALTER TABLE clients ADD UNIQUE (agency_id, id)")
    op.execute("CREATE INDEX clients_agency_idx ON clients (agency_id)")
    op.execute("""
        CREATE TABLE agency_memberships (
            agency_id uuid NOT NULL REFERENCES agencies(id),
            user_id uuid NOT NULL REFERENCES users(id),
            role text NOT NULL CHECK (role IN ('admin','operator','approver','viewer')),
            all_clients boolean NOT NULL DEFAULT false,
            active boolean NOT NULL DEFAULT true,
            PRIMARY KEY (agency_id, user_id)
        )
    """)
    op.execute(f"""
        INSERT INTO agency_memberships (agency_id, user_id, role, all_clients, active)
        SELECT '{DEFAULT_AGENCY}', id, role, client_id IS NULL, active FROM users
    """)
    op.execute("""
        CREATE TABLE client_assignments (
            agency_id uuid NOT NULL,
            user_id uuid NOT NULL,
            client_id uuid NOT NULL,
            PRIMARY KEY (agency_id, user_id, client_id),
            FOREIGN KEY (agency_id, user_id) REFERENCES agency_memberships(agency_id, user_id),
            FOREIGN KEY (agency_id, client_id) REFERENCES clients(agency_id, id)
        )
    """)
    op.execute(f"""
        INSERT INTO client_assignments (agency_id, user_id, client_id)
        SELECT '{DEFAULT_AGENCY}', id, client_id FROM users WHERE client_id IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE external_login_identities (
            issuer text NOT NULL,
            subject text NOT NULL,
            user_id uuid NOT NULL REFERENCES users(id),
            PRIMARY KEY (issuer, subject)
        )
    """)
    op.execute("ALTER TABLE admin_sessions ADD COLUMN agency_id uuid")
    op.execute(f"UPDATE admin_sessions SET agency_id = '{DEFAULT_AGENCY}'")
    op.execute(f"ALTER TABLE admin_sessions ALTER COLUMN agency_id SET DEFAULT '{DEFAULT_AGENCY}'")
    op.execute("ALTER TABLE admin_sessions ALTER COLUMN agency_id SET NOT NULL")
    op.execute("ALTER TABLE admin_sessions ADD FOREIGN KEY (agency_id) REFERENCES agencies(id)")


def downgrade() -> None:
    # Legacy global users and kill flags cannot represent multiple agencies safely.
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agencies WHERE id <> '{DEFAULT_AGENCY}') THEN
                RAISE EXCEPTION 'Cannot downgrade a multi-agency installation';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE admin_sessions DROP COLUMN agency_id")
    op.execute("DROP TABLE external_login_identities")
    op.execute("DROP TABLE client_assignments")
    op.execute("DROP TABLE agency_memberships")
    op.execute("ALTER TABLE clients DROP COLUMN agency_id")
    op.execute("DROP TABLE agencies")
