"""Shared ownership DDL for metadata-created development/test databases."""

# Ordered so parents are backfilled before children. Historical migrations keep their own snapshot.
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


def trigger_sql(table: str, parent: str, field: str) -> str:
    return (
        f"CREATE TRIGGER {table}_agency BEFORE INSERT OR UPDATE ON public.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION public.governflow_derive_agency('{parent}', '{field}')"
    )
