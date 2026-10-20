"""Durable provider-call lifecycle, charge evidence, and recovery jobs."""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

TABLES = ("provider_calls", "charge_entries", "accounting_receipts", "accounting_recovery")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reservations ADD CONSTRAINT reservations_call_parent_key "
        "UNIQUE (agency_id,id,budget_id,run_id)"
    )
    op.execute(
        "ALTER TABLE budgets ADD CONSTRAINT budgets_call_parent_key "
        "UNIQUE(agency_id,workflow_id,id)"
    )
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_call_parent_key UNIQUE(agency_id,workflow_id,id)"
    )
    op.execute("""CREATE TABLE provider_calls (
	id UUID NOT NULL, 
	agency_id UUID NOT NULL, 
	client_id UUID NOT NULL, 
	workflow_id UUID NOT NULL, 
	reservation_id UUID NOT NULL, 
	budget_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	period TEXT NOT NULL, 
	provider TEXT, 
	model TEXT, 
	state TEXT DEFAULT 'reserved' NOT NULL, 
	estimate_micro BIGINT NOT NULL, 
	legacy_baseline_micro BIGINT,
	charged_micro BIGINT DEFAULT '0' NOT NULL, 
	revision INTEGER DEFAULT '0' NOT NULL, 
	price_snapshot JSONB DEFAULT '{}' NOT NULL, 
	usage JSONB, 
	provider_request_id TEXT, 
	provenance TEXT DEFAULT 'gateway' NOT NULL, 
	uncertainty_reason TEXT, 
	dispatch_at TIMESTAMP WITH TIME ZONE, 
	lease_until TIMESTAMP WITH TIME ZONE NOT NULL, 
	lease_owner UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (agency_id, id), 
	UNIQUE (reservation_id), 
	FOREIGN KEY(agency_id, client_id) REFERENCES clients (agency_id, id), 
	FOREIGN KEY(agency_id, client_id, workflow_id) REFERENCES workflows (agency_id, client_id, id), 
	FOREIGN KEY(agency_id, reservation_id, budget_id, run_id)
        REFERENCES reservations (agency_id, id, budget_id, run_id), 
	CHECK (state IN ('reserved','dispatched','settled_estimated',
        'settled_measured','voided')), 
	CHECK (estimate_micro > 0 AND charged_micro >= 0 AND revision >= 0), 
	FOREIGN KEY(agency_id) REFERENCES agencies (id)
)""")
    op.execute("CREATE INDEX calls_lease_idx ON provider_calls (state, lease_until)")
    op.execute(
        "CREATE INDEX calls_scope_created_idx ON provider_calls "
        "(agency_id, client_id, created_at, id)"
    )
    op.execute(
        "ALTER TABLE provider_calls ADD FOREIGN KEY(agency_id,workflow_id,budget_id) "
        "REFERENCES budgets(agency_id,workflow_id,id)"
    )
    op.execute(
        "ALTER TABLE provider_calls ADD FOREIGN KEY(agency_id,workflow_id,run_id) "
        "REFERENCES runs(agency_id,workflow_id,id)"
    )
    op.execute("""CREATE TABLE charge_entries (
	id UUID NOT NULL, 
	agency_id UUID NOT NULL, 
	call_id UUID NOT NULL, 
	evidence_id UUID NOT NULL, 
	revision INTEGER NOT NULL, 
	delta_micro BIGINT NOT NULL, 
	amount_micro BIGINT NOT NULL, 
	kind TEXT NOT NULL, 
	evidence_ref TEXT, 
	actor TEXT NOT NULL, 
	reason TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (agency_id, evidence_id), 
	UNIQUE (call_id, revision), 
	FOREIGN KEY(agency_id, call_id) REFERENCES provider_calls (agency_id, id), 
	CHECK (amount_micro >= 0 AND revision > 0)
)""")
    op.execute("""CREATE TABLE accounting_receipts (
	agency_id UUID NOT NULL, 
	evidence_id UUID NOT NULL, 
	call_id UUID NOT NULL, 
	checksum BYTEA NOT NULL, 
	disposition TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (agency_id, evidence_id), 
	FOREIGN KEY(agency_id, call_id) REFERENCES provider_calls (agency_id, id)
)""")
    op.execute("""CREATE TABLE accounting_recovery (
	agency_id UUID NOT NULL, 
	evidence_id UUID NOT NULL, 
	client_id UUID NOT NULL, 
	call_id UUID NOT NULL, 
	checksum BYTEA NOT NULL, 
	payload JSONB NOT NULL, 
	status TEXT DEFAULT 'pending' NOT NULL, 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	next_attempt TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	lease_owner UUID, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (agency_id, evidence_id), 
	FOREIGN KEY(agency_id, client_id) REFERENCES clients (agency_id, id), 
	CHECK (status IN ('pending','running','retry','quarantined','complete'))
)""")
    op.execute(
        "CREATE INDEX accounting_recovery_due_idx ON accounting_recovery (status, next_attempt)"
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY agency_isolation ON {table} "
            "USING (agency_id=NULLIF(current_setting('governflow.agency_id',true),'')::uuid) "
            "WITH CHECK (agency_id=NULLIF(current_setting('governflow.agency_id',true),'')::uuid)"
        )
        grants = (
            "SELECT, INSERT"
            if table in ("charge_entries", "accounting_receipts")
            else "SELECT, INSERT, UPDATE"
        )
        op.execute(f"GRANT {grants} ON {table} TO governflow_runtime")
    op.execute("""CREATE OR REPLACE FUNCTION public.accounting_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN
RAISE EXCEPTION 'accounting evidence is append-only'; END $$""")
    for table in ("charge_entries", "accounting_receipts"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION public.accounting_immutable()"
        )
    # Historical completed rows and financial totals stay untouched. Pending legacy calls may
    # already have reached a provider: only late evidence can establish a safe zero charge.
    op.execute("""INSERT INTO provider_calls
        (id,agency_id,client_id,workflow_id,reservation_id,budget_id,run_id,period,state,
         estimate_micro,provenance,uncertainty_reason,lease_until,dispatch_at,created_at)
        SELECT r.id,r.agency_id,w.client_id,w.id,r.id,b.id,r.run_id,b.period,'dispatched',
               r.est_micro,'legacy_pending','legacy_dispatch_unknown',now(),r.created_at,r.created_at
        FROM reservations r JOIN budgets b ON b.id=r.budget_id
        JOIN workflows w ON w.id=b.workflow_id WHERE r.status='reserved'""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM charge_entries) OR
        EXISTS (SELECT 1 FROM accounting_recovery) OR
        EXISTS (SELECT 1 FROM provider_calls WHERE provenance<>'legacy_pending') THEN
        RAISE EXCEPTION 'Cannot downgrade active provider accounting; restore the upgrade backup';
        END IF; END $$""")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION public.accounting_immutable()")
    op.execute("ALTER TABLE reservations DROP CONSTRAINT reservations_call_parent_key")
    op.execute("ALTER TABLE budgets DROP CONSTRAINT budgets_call_parent_key")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_call_parent_key")
