"""Expand, backfill, and validate explicit agency ownership."""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

PARENTS = {
    "workflows": ("clients", "client_id"),
    "provider_credentials": ("clients", "client_id"),
    "provider_accounts": ("clients", "client_id"),
    "reconcile_snapshots": ("clients", "client_id"),
    "reconcile_jobs": ("clients", "client_id"),
    "monthly_metrics": ("clients", "client_id"),
    "virtual_keys": ("workflows", "workflow_id"),
    "budgets": ("workflows", "workflow_id"),
    "runs": ("workflows", "workflow_id"),
    "rate_buckets": ("workflows", "workflow_id"),
    "approvals": ("workflows", "workflow_id"),
    "reservations": ("budgets", "budget_id"),
    "events": ("clients", "client_id"),
    "alerts": ("clients", "client_id"),
    "notifications": ("approvals", "approval_id"),
}
SECONDARY = {
    "reservations": [("runs", "run_id")],
    "approvals": [("runs", "run_id")],
    "events": [("runs", "run_id")],
    "alerts": [("workflows", "workflow_id")],
}
DERIVE_FUNCTION = """
CREATE OR REPLACE FUNCTION public.governflow_derive_agency() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE owner_id uuid;
BEGIN
    EXECUTE format('SELECT agency_id FROM public.%I WHERE id = $1', TG_ARGV[0])
        INTO owner_id USING (to_jsonb(NEW)->>TG_ARGV[1])::uuid;
    IF owner_id IS NULL OR (NEW.agency_id IS NOT NULL AND NEW.agency_id <> owner_id) THEN
        RAISE EXCEPTION 'Agency ownership does not match parent' USING ERRCODE = '23503';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.agency_id IS DISTINCT FROM OLD.agency_id THEN
        RAISE EXCEPTION 'Agency ownership is immutable' USING ERRCODE = '23503';
    END IF;
    NEW.agency_id := owner_id;
    RETURN NEW;
END $$
"""


def upgrade() -> None:
    # Preserve orphaned legacy audit identifiers without editing audit payloads.
    op.execute("""INSERT INTO clients (id, name, status)
        SELECT DISTINCT e.client_id, 'Recovered legacy client', 'archived' FROM events e
        WHERE NOT EXISTS (SELECT 1 FROM clients c WHERE c.id = e.client_id)""")
    for table, (parent, field) in PARENTS.items():
        op.execute(f"ALTER TABLE {table} ADD COLUMN agency_id uuid")
        if table == "events":
            op.execute("ALTER TABLE events DISABLE TRIGGER events_no_change")
        op.execute(
            f"UPDATE {table} t SET agency_id = p.agency_id FROM {parent} p WHERE p.id = t.{field}"
        )
        if table == "events":
            op.execute("ALTER TABLE events ENABLE TRIGGER events_no_change")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN agency_id SET NOT NULL")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN agency_id SET DEFAULT "
            "NULLIF(current_setting('governflow.agency_id', true), '')::uuid"
        )
        op.execute(f"ALTER TABLE {table} ADD FOREIGN KEY (agency_id) REFERENCES agencies(id)")
        op.execute(f"CREATE INDEX {table}_agency_idx ON {table} (agency_id)")
    for table in ("workflows", "budgets", "runs", "approvals"):
        op.execute(f"ALTER TABLE {table} ADD UNIQUE (agency_id, id)")
    for table, (parent, field) in PARENTS.items():
        for target, column in [(parent, field), *SECONDARY.get(table, [])]:
            name = f"{table}_{column}_agency_fk"
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (agency_id, {column}) "
                f"REFERENCES {target}(agency_id, id) NOT VALID"
            )
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        # Keep the existing secondary relationship checks and add an explicit ownership check.
        op.execute(
            f"CREATE POLICY explicit_agency ON {table} AS RESTRICTIVE "
            "USING (agency_id = NULLIF(current_setting('governflow.agency_id', true), '')::uuid) "
            "WITH CHECK (agency_id = "
            "NULLIF(current_setting('governflow.agency_id', true), '')::uuid)"
        )
    op.execute(DERIVE_FUNCTION)
    for table, (parent, field) in PARENTS.items():
        op.execute(
            f"CREATE TRIGGER {table}_agency BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION governflow_derive_agency('{parent}', '{field}')"
        )
    # Scope IDs are legacy text, so use typed references alongside them for FK enforcement.
    op.execute("ALTER TABLE kill_flags ADD COLUMN agency_id uuid REFERENCES agencies(id)")
    op.execute("ALTER TABLE kill_flags ADD COLUMN client_id uuid")
    op.execute("ALTER TABLE kill_flags ADD COLUMN workflow_id uuid")
    op.execute(
        "UPDATE kill_flags k SET agency_id=c.agency_id, client_id=c.id "
        "FROM clients c WHERE k.scope='client' AND k.scope_id=c.id::text"
    )
    op.execute(
        "UPDATE kill_flags k SET agency_id=w.agency_id, workflow_id=w.id "
        "FROM workflows w WHERE k.scope='workflow' AND k.scope_id=w.id::text"
    )
    op.execute(
        "ALTER TABLE kill_flags ADD FOREIGN KEY (agency_id, client_id) "
        "REFERENCES clients(agency_id,id)"
    )
    op.execute(
        "ALTER TABLE kill_flags ADD FOREIGN KEY (agency_id, workflow_id) "
        "REFERENCES workflows(agency_id,id)"
    )
    op.execute("""ALTER TABLE kill_flags ADD CONSTRAINT kill_ownership CHECK (
        (scope='global' AND scope_id='' AND agency_id IS NULL
         AND client_id IS NULL AND workflow_id IS NULL)
        OR (scope='client' AND agency_id IS NOT NULL AND client_id IS NOT NULL
            AND scope_id=client_id::text AND workflow_id IS NULL)
        OR (scope='workflow' AND agency_id IS NOT NULL AND workflow_id IS NOT NULL
            AND scope_id=workflow_id::text AND client_id IS NULL))""")
    op.execute("""CREATE FUNCTION governflow_kill_owner() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog, public AS $$ BEGIN
        IF NEW.scope='client' THEN
            NEW.client_id := NEW.scope_id::uuid;
            SELECT agency_id INTO NEW.agency_id FROM public.clients WHERE id=NEW.client_id;
        ELSIF NEW.scope='workflow' THEN
            NEW.workflow_id := NEW.scope_id::uuid;
            SELECT agency_id INTO NEW.agency_id FROM public.workflows WHERE id=NEW.workflow_id;
        END IF;
        RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER kill_owner BEFORE INSERT OR UPDATE ON kill_flags "
        "FOR EACH ROW EXECUTE FUNCTION governflow_kill_owner()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER kill_owner ON kill_flags")
    op.execute("DROP FUNCTION governflow_kill_owner()")
    op.execute(
        "ALTER TABLE kill_flags DROP COLUMN agency_id CASCADE, DROP COLUMN client_id CASCADE, "
        "DROP COLUMN workflow_id CASCADE"
    )
    for table in reversed(PARENTS):
        op.execute(f"DROP TRIGGER {table}_agency ON {table}")
        op.execute(f"DROP POLICY explicit_agency ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN agency_id CASCADE")
    op.execute("DROP FUNCTION governflow_derive_agency()")
