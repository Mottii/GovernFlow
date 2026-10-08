"""Isolate tenant data under a restricted runtime role and transaction-local agency context."""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

ROLE = "governflow_runtime"
AGENCY = "NULLIF(current_setting('governflow.agency_id', true), '')::uuid"
CLIENT = (
    f"EXISTS (SELECT 1 FROM public.clients c WHERE c.id = client_id AND c.agency_id = {AGENCY})"
)
WORKFLOW = (
    "EXISTS (SELECT 1 FROM public.workflows w JOIN public.clients c ON c.id = w.client_id "
    f"WHERE w.id = workflow_id AND c.agency_id = {AGENCY})"
)
# Existing parent foreign keys propagate client ownership to workflow-owned rows.
POLICIES = {
    "clients": f"agency_id = {AGENCY}",
    "agency_memberships": f"agency_id = {AGENCY}",
    "client_assignments": f"agency_id = {AGENCY}",
    "admin_sessions": f"agency_id = {AGENCY}",
    "workflows": CLIENT,
    "provider_credentials": CLIENT,
    "provider_accounts": CLIENT,
    "reconcile_snapshots": CLIENT,
    "reconcile_jobs": CLIENT,
    "monthly_metrics": CLIENT,
    "events": CLIENT + " AND (run_id IS NULL OR EXISTS (SELECT 1 FROM public.runs r "
    "JOIN public.workflows w ON w.id = r.workflow_id "
    "WHERE r.id = events.run_id AND w.client_id = events.client_id))",
    "alerts": CLIENT + " AND (workflow_id IS NULL OR EXISTS (SELECT 1 FROM public.workflows w "
    "WHERE w.id = alerts.workflow_id AND w.client_id = alerts.client_id))",
    "virtual_keys": WORKFLOW,
    "budgets": WORKFLOW,
    "runs": WORKFLOW,
    "rate_buckets": WORKFLOW,
    "approvals": WORKFLOW + " AND (run_id IS NULL OR EXISTS (SELECT 1 FROM public.runs r "
    "WHERE r.id = approvals.run_id AND r.workflow_id = approvals.workflow_id))",
    "reservations": (
        "EXISTS (SELECT 1 FROM public.budgets b JOIN public.runs r "
        "ON r.workflow_id = b.workflow_id JOIN public.workflows w ON w.id = b.workflow_id "
        "JOIN public.clients c ON c.id = w.client_id "
        f"WHERE b.id = budget_id AND r.id = run_id AND c.agency_id = {AGENCY})"
    ),
    "notifications": (
        "EXISTS (SELECT 1 FROM public.approvals a JOIN public.workflows w ON w.id = a.workflow_id "
        "JOIN public.clients c ON c.id = w.client_id "
        f"WHERE a.id = approval_id AND c.agency_id = {AGENCY})"
    ),
    "replay_receipts": f"agency_id = {AGENCY}",
    "webhook_receipts": f"agency_id = {AGENCY}",
}
KILL = (
    "(scope = 'client' AND EXISTS (SELECT 1 FROM public.clients c "
    f"WHERE c.id::text = scope_id AND c.agency_id = {AGENCY})) OR "
    "(scope = 'workflow' AND EXISTS (SELECT 1 FROM public.workflows w "
    "JOIN public.clients c ON c.id = w.client_id "
    f"WHERE w.id::text = scope_id AND c.agency_id = {AGENCY}))"
)


def upgrade() -> None:
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOREPLICATION NOBYPASSRLS;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}' AND
                       (rolsuper OR rolbypassrls OR rolcreatedb OR
                        rolcreaterole OR rolreplication OR rolcanlogin)) THEN
                RAISE EXCEPTION 'governflow_runtime must be an unprivileged role';
            END IF;
            IF pg_has_role('{ROLE}',
                    (SELECT datdba FROM pg_database WHERE datname = current_database()), 'USAGE')
                OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND pg_has_role('{ROLE}', c.relowner, 'USAGE')) THEN
                RAISE EXCEPTION 'governflow_runtime must not inherit database or table ownership';
            END IF;
        END $$
    """)
    for table in ("replay_receipts", "webhook_receipts"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN agency_id uuid")
        op.execute(f"UPDATE {table} SET agency_id = '00000000-0000-0000-0000-000000000001'")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN agency_id SET DEFAULT {AGENCY}")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN agency_id SET NOT NULL")
        op.execute(f"ALTER TABLE {table} ADD FOREIGN KEY (agency_id) REFERENCES agencies(id)")
    for table, condition in POLICIES.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY agency_isolation ON public.{table} "
            f"USING ({condition}) WITH CHECK ({condition})"
        )
    op.execute("ALTER TABLE public.kill_flags ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agency_read ON public.kill_flags FOR SELECT "
        f"USING (scope = 'global' OR {KILL})"
    )
    op.execute(f"CREATE POLICY agency_insert ON public.kill_flags FOR INSERT WITH CHECK ({KILL})")
    op.execute(
        "CREATE POLICY agency_update ON public.kill_flags FOR UPDATE "
        f"USING ({KILL}) WITH CHECK ({KILL})"
    )
    op.execute(f"CREATE POLICY agency_delete ON public.kill_flags FOR DELETE USING ({KILL})")
    # Narrow authentication lookups run as the migration owner, never as the application login.
    # Schema-qualified tables and a fixed search path prevent search-path replacement attacks.
    op.execute("""
        CREATE FUNCTION public.governflow_key_agency(digest bytea) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT c.agency_id FROM public.virtual_keys k
            JOIN public.workflows w ON w.id = k.workflow_id
            JOIN public.clients c ON c.id = w.client_id
            JOIN public.agencies a ON a.id = c.agency_id
            WHERE k.key_hash = digest AND k.revoked_at IS NULL
              AND w.status = 'active' AND c.status = 'active' AND a.status = 'active'
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.governflow_session_agency(digest bytea) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT s.agency_id FROM public.admin_sessions s
            JOIN public.users u ON u.id = s.user_id
            JOIN public.agency_memberships m ON m.user_id = u.id AND m.agency_id = s.agency_id
            JOIN public.agencies a ON a.id = m.agency_id
            WHERE s.token_hash = digest AND s.expires_at > now()
              AND u.active AND m.active AND a.status = 'active'
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.governflow_login_agencies(identity uuid)
        RETURNS TABLE (agency_id uuid, role text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT m.agency_id, m.role FROM public.agency_memberships m
            JOIN public.agencies a ON a.id = m.agency_id
            JOIN public.users u ON u.id = m.user_id
            WHERE m.user_id = identity AND m.active AND u.active AND a.status = 'active'
        $$
    """)
    for function in (
        "governflow_key_agency(bytea)",
        "governflow_session_agency(bytea)",
        "governflow_login_agencies(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function} TO {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    # Shared identity/authentication data is global. Tenant grants remain RLS protected.
    tables = [*POLICIES, "kill_flags", "users", "login_buckets"]
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(tables)} TO {ROLE}")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON public.prices FROM PUBLIC")
    op.execute(f"GRANT SELECT ON public.agencies, public.prices, public.alembic_version TO {ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE public.events_id_seq TO {ROLE}")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agencies
                       WHERE id <> '00000000-0000-0000-0000-000000000001') THEN
                RAISE EXCEPTION 'Cannot downgrade a multi-agency installation';
            END IF;
        END $$
    """)
    for function in (
        "governflow_key_agency(bytea)",
        "governflow_session_agency(bytea)",
        "governflow_login_agencies(uuid)",
    ):
        op.execute(f"DROP FUNCTION public.{function}")
    for table in POLICIES:
        op.execute(f"DROP POLICY agency_isolation ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
    for name in ("agency_read", "agency_insert", "agency_update", "agency_delete"):
        op.execute(f"DROP POLICY {name} ON public.kill_flags")
    op.execute("ALTER TABLE public.kill_flags DISABLE ROW LEVEL SECURITY")
    for table in ("replay_receipts", "webhook_receipts"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN agency_id")
    # Cluster roles are shared across databases. Keep the NOLOGIN group on downgrade.
