"""Agency budgets, policy revisions, credential versions and administration history.

Revision ID: 0020
Revises: 0019
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE provider_credentials ADD CONSTRAINT "
        "credential_agency_identity UNIQUE(agency_id,id)"
    )
    op.execute("ALTER TABLE provider_credentials ADD COLUMN encryption_key_id TEXT")
    op.execute("ALTER TABLE workflows ADD COLUMN policy_version INTEGER DEFAULT '0' NOT NULL")
    op.execute(
        "ALTER TABLE workflows ADD COLUMN policy_review_required BOOLEAN DEFAULT 'false' NOT NULL"
    )
    op.execute("ALTER TABLE provider_credentials ADD COLUMN active_version_id UUID")
    op.execute("ALTER TABLE budgets ADD COLUMN revision INTEGER DEFAULT '1' NOT NULL")
    op.execute("ALTER TABLE budgets ADD COLUMN template_revision_id UUID")
    op.execute("ALTER TABLE runs ADD COLUMN stopped BOOLEAN DEFAULT 'false' NOT NULL")
    op.execute("ALTER TABLE runs ADD COLUMN stop_reason TEXT")
    op.execute("ALTER TABLE runs ADD COLUMN stop_revision INTEGER DEFAULT '0' NOT NULL")
    op.execute("ALTER TABLE reservations ADD COLUMN client_budget_id UUID")
    op.execute("ALTER TABLE approvals ADD COLUMN policy_version INTEGER DEFAULT '0' NOT NULL")
    op.execute("ALTER TABLE approvals ADD COLUMN matched_rules JSONB DEFAULT '[]' NOT NULL")
    op.execute("ALTER TABLE approvals ADD COLUMN invalidated_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE approvals ADD COLUMN encryption_key_id TEXT")
    op.execute("ALTER TABLE provider_accounts ADD COLUMN validated_credential_version_id UUID")
    op.execute("ALTER TABLE integration_destinations ADD COLUMN encryption_key_id TEXT")
    op.execute("ALTER TABLE provider_calls ADD COLUMN client_budget_id UUID")
    op.execute("ALTER TABLE provider_calls ADD COLUMN policy_version INTEGER DEFAULT '0' NOT NULL")
    op.execute("ALTER TABLE provider_calls ADD COLUMN credential_version_id UUID")
    op.execute(
        "CREATE TABLE client_budgets (\n        id UUID NOT NULL,"
        " \n        agency_id UUID NOT NULL, \n        client_id UUID NOT "
        "NULL, \n        period TEXT NOT NULL, \n        cap_micro BIGINT,"
        " \n        spent_micro BIGINT DEFAULT '0' NOT NULL, \n        "
        "reserved_micro BIGINT DEFAULT '0' NOT NULL, \n        revision "
        "INTEGER DEFAULT '1' NOT NULL, \n        template_revision_id "
        "UUID, \n        PRIMARY KEY (id), \n        UNIQUE (agency_id, "
        "id), \n        UNIQUE (client_id, period), \n        FOREIGN "
        "KEY(agency_id, client_id) REFERENCES clients (agency_id, id),"
        " \n        CHECK (spent_micro>=0 AND reserved_micro>=0 AND "
        "(cap_micro IS NULL OR cap_micro>=0)), \n        UNIQUE "
        "(agency_id, client_id, period, id)\n)"
    )
    op.execute(
        "CREATE TABLE budget_templates (\n        id UUID NOT NULL,"
        " \n        agency_id UUID NOT NULL, \n        client_id UUID NOT "
        "NULL, \n        workflow_id UUID, \n        scope TEXT NOT NULL,"
        " \n        scope_id UUID NOT NULL, \n        revision INTEGER NOT "
        "NULL, \n        cap_micro BIGINT NOT NULL, \n        "
        "effective_period TEXT NOT NULL, \n        enabled BOOLEAN NOT "
        "NULL, \n        actor TEXT NOT NULL, \n        created_at "
        "TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n        "
        "PRIMARY KEY (id), \n        UNIQUE (agency_id, id), \n        "
        "UNIQUE (agency_id, scope, scope_id, revision), \n        FOREIGN "
        "KEY(agency_id, client_id) REFERENCES clients (agency_id, id),"
        " \n        FOREIGN KEY(agency_id, workflow_id) REFERENCES "
        "workflows (agency_id, id), \n        CHECK (scope IN "
        "('client','workflow') AND cap_micro>=0 AND revision>0),"
        " \n        CHECK ((scope='client' AND workflow_id IS NULL AND "
        "scope_id=client_id) OR (scope='workflow' AND workflow_id IS NOT "
        "NULL AND scope_id=workflow_id))\n)"
    )
    op.execute(
        "CREATE TABLE policy_revisions (\n        id UUID NOT NULL,"
        " \n        agency_id UUID NOT NULL, \n        client_id UUID NOT "
        "NULL, \n        workflow_id UUID NOT NULL, \n        version "
        "INTEGER NOT NULL, \n        risk_class TEXT NOT NULL, \n        "
        "policy JSONB NOT NULL, \n        content_hash BYTEA NOT NULL,"
        " \n        actor TEXT NOT NULL, \n        source_version INTEGER,"
        " \n        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT "
        "NULL, \n        PRIMARY KEY (id), \n        UNIQUE (agency_id, "
        "id), \n        UNIQUE (workflow_id, version), \n        FOREIGN "
        "KEY(agency_id, workflow_id) REFERENCES workflows (agency_id, "
        "id), \n        FOREIGN KEY(agency_id, client_id) REFERENCES "
        "clients (agency_id, id)\n)"
    )
    op.execute(
        "CREATE TABLE credential_versions (\n        id UUID NOT NULL,"
        " \n        agency_id UUID NOT NULL, \n        credential_id UUID "
        "NOT NULL, \n        version INTEGER NOT NULL, \n        state TEXT"
        " NOT NULL, \n        ciphertext BYTEA NOT NULL, \n        nonce "
        "BYTEA NOT NULL, \n        encryption_key_id TEXT, \n        actor "
        "TEXT NOT NULL, \n        created_at TIMESTAMP WITH TIME ZONE "
        "DEFAULT now() NOT NULL, \n        PRIMARY KEY (id), \n        "
        "UNIQUE (agency_id, id), \n        UNIQUE (credential_id, "
        "version), \n        FOREIGN KEY(agency_id, credential_id) "
        "REFERENCES provider_credentials (agency_id, id), \n        CHECK "
        "(state IN ('staged','active','retired'))\n)"
    )
    op.execute(
        "CREATE TABLE administrative_events (\n        id BIGSERIAL NOT "
        "NULL, \n        agency_id UUID NOT NULL, \n        actor TEXT NOT "
        "NULL, \n        resource TEXT NOT NULL, \n        resource_id TEXT"
        " NOT NULL, \n        action TEXT NOT NULL, \n        details JSONB"
        " NOT NULL, \n        previous_hash BYTEA NOT NULL, \n        hash "
        "BYTEA NOT NULL, \n        created_at TIMESTAMP WITH TIME ZONE NOT"
        " NULL, \n        PRIMARY KEY (id), \n        FOREIGN "
        "KEY(agency_id) REFERENCES agencies (id)\n)"
    )
    op.execute(
        "CREATE INDEX administrative_events_agency_sequence ON "
        "administrative_events (agency_id, id)"
    )
    op.execute("ALTER TABLE client_budgets ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agency_isolation ON client_budgets USING (agency_i"
        "d=NULLIF(current_setting('governflow.agency_id',true),'')::uuid)"
        " WITH CHECK (agency_id=NULLIF(current_setting('governflow.agency"
        "_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON client_budgets TO governflow_runtime")
    op.execute("ALTER TABLE budget_templates ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agency_isolation ON budget_templates USING (agency"
        "_id=NULLIF(current_setting('governflow.agency_id',true),'')::uui"
        "d) WITH CHECK (agency_id=NULLIF(current_setting('governflow.agen"
        "cy_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT, INSERT ON budget_templates TO governflow_runtime")
    op.execute(
        "CREATE TRIGGER budget_templates_immutable BEFORE UPDATE OR "
        "DELETE ON budget_templates FOR EACH ROW EXECUTE FUNCTION "
        "public.accounting_immutable()"
    )
    op.execute("ALTER TABLE policy_revisions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agency_isolation ON policy_revisions USING (agency"
        "_id=NULLIF(current_setting('governflow.agency_id',true),'')::uui"
        "d) WITH CHECK (agency_id=NULLIF(current_setting('governflow.agen"
        "cy_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT, INSERT ON policy_revisions TO governflow_runtime")
    op.execute(
        "CREATE TRIGGER policy_revisions_immutable BEFORE UPDATE OR "
        "DELETE ON policy_revisions FOR EACH ROW EXECUTE FUNCTION "
        "public.accounting_immutable()"
    )
    op.execute("ALTER TABLE credential_versions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agency_isolation ON credential_versions USING (age"
        "ncy_id=NULLIF(current_setting('governflow.agency_id',true),'')::"
        "uuid) WITH CHECK (agency_id=NULLIF(current_setting('governflow.a"
        "gency_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON credential_versions TO governflow_runtime")
    op.execute("ALTER TABLE administrative_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agency_isolation ON administrative_events USING (a"
        "gency_id=NULLIF(current_setting('governflow.agency_id',true),'')"
        "::uuid) WITH CHECK (agency_id=NULLIF(current_setting('governflow"
        ".agency_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT, INSERT ON administrative_events TO governflow_runtime")
    op.execute(
        "CREATE TRIGGER administrative_events_immutable BEFORE UPDATE OR "
        "DELETE ON administrative_events FOR EACH ROW EXECUTE FUNCTION "
        "public.accounting_immutable()"
    )
    op.execute("GRANT USAGE, SELECT ON SEQUENCE administrative_events_id_seq TO governflow_runtime")
    op.execute(
        "INSERT INTO client_budgets(id,agency_id,client_id,period,spent_m"
        "icro,reserved_micro)\nSELECT gen_random_uuid(),w.agency_id,w.clie"
        "nt_id,b.period,SUM(b.spent_micro),SUM(b.reserved_micro)\nFROM "
        "budgets b JOIN workflows w ON w.id=b.workflow_id GROUP BY "
        "w.agency_id,w.client_id,b.period"
    )
    op.execute(
        "UPDATE reservations r SET client_budget_id=c.id FROM budgets "
        "b,workflows w,client_budgets c\nWHERE b.id=r.budget_id AND "
        "w.id=b.workflow_id AND c.client_id=w.client_id AND "
        "c.period=b.period"
    )
    op.execute(
        "UPDATE provider_calls p SET client_budget_id=r.client_budget_id "
        "FROM reservations r WHERE r.id=p.reservation_id"
    )
    op.execute(
        "INSERT INTO credential_versions(id,agency_id,credential_id,versi"
        "on,state,ciphertext,nonce,actor)\nSELECT gen_random_uuid(),agency"
        "_id,id,1,'active',ciphertext,nonce,'migration' FROM "
        "provider_credentials"
    )
    op.execute(
        "UPDATE provider_credentials p SET active_version_id=v.id FROM "
        "credential_versions v WHERE v.credential_id=p.id"
    )
    op.execute(
        "UPDATE provider_accounts a SET "
        "validated_credential_version_id=p.active_version_id FROM "
        "provider_credentials p\nWHERE p.client_id=a.client_id AND "
        "p.provider=a.provider AND p.label=a.credential_label AND "
        "a.validated_at IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE reservations ADD FOREIGN "
        "KEY(agency_id,client_budget_id) REFERENCES "
        "client_budgets(agency_id,id)"
    )
    op.execute(
        "ALTER TABLE provider_calls ADD FOREIGN "
        "KEY(agency_id,client_budget_id) REFERENCES "
        "client_budgets(agency_id,id)"
    )
    op.execute(
        "ALTER TABLE provider_calls ADD FOREIGN "
        "KEY(agency_id,client_id,period,client_budget_id) REFERENCES "
        "client_budgets(agency_id,client_id,period,id)"
    )
    op.execute(
        "CREATE TABLE deployment_keys(kind TEXT NOT NULL,key_id TEXT NOT "
        "NULL,fingerprint TEXT NOT NULL,retired_at "
        "TIMESTAMPTZ,verify_until TIMESTAMPTZ,PRIMARY KEY(kind,key_id))"
    )
    op.execute("GRANT SELECT ON deployment_keys TO governflow_runtime")
    # Pre-upgrade review links/tokens used the legacy key even before registry metadata existed.
    op.execute(
        "INSERT INTO deployment_keys(kind,key_id,fingerprint,retired_at,verify_until) "
        "SELECT 'signing','0000','',clock_timestamp(),"
        "clock_timestamp()+interval '7 days 5 minutes' "
        "WHERE EXISTS(SELECT 1 FROM approvals)"
    )
    op.execute(
        "\nCREATE OR REPLACE FUNCTION public.credential_version_guard() "
        "RETURNS trigger\nLANGUAGE plpgsql SET "
        "search_path=pg_catalog,public AS $$\nBEGIN\nIF TG_OP='DELETE' THEN"
        " RAISE EXCEPTION 'credential versions are retained'; END IF;\nIF "
        "(to_jsonb(OLD)-"
        "ARRAY['state','ciphertext','nonce','encryption_key_id']) IS "
        "DISTINCT FROM\n   (to_jsonb(NEW)-"
        "ARRAY['state','ciphertext','nonce','encryption_key_id']) THEN"
        "\n   RAISE EXCEPTION 'credential version identity is immutable';\n"
        "END IF;\nIF OLD.state='retired' AND NEW.state<>'retired' THEN\n   "
        "RAISE EXCEPTION 'retired credential cannot be activated';\nEND "
        "IF;\nIF OLD.state='active' AND NEW.state='staged' THEN\n   RAISE "
        "EXCEPTION 'active credential cannot return to staging';\nEND IF;\n"
        "IF (OLD.ciphertext,OLD.nonce,OLD.encryption_key_id) IS DISTINCT "
        "FROM\n   (NEW.ciphertext,NEW.nonce,NEW.encryption_key_id) AND "
        "current_user <>\n   (SELECT pg_get_userbyid(relowner) FROM "
        "pg_class WHERE oid=TG_RELID) THEN\n   RAISE EXCEPTION 'credential"
        " re-encryption requires maintenance';\nEND IF;\nRETURN NEW;\nEND $$"
        "\n"
    )
    op.execute(
        "CREATE TRIGGER credential_version_immutable BEFORE UPDATE OR "
        "DELETE ON credential_versions FOR EACH ROW EXECUTE FUNCTION "
        "public.credential_version_guard()"
    )
    op.execute("ALTER TABLE policy_revisions ADD COLUMN legacy_source JSONB")
    op.execute("ALTER TABLE credential_versions ADD UNIQUE(agency_id,credential_id,id)")
    op.execute(
        "ALTER TABLE provider_credentials ADD CONSTRAINT credential_active_version_fk "
        "FOREIGN KEY(agency_id,id,active_version_id) "
        "REFERENCES credential_versions(agency_id,credential_id,id)"
    )
    for table in ("policy_revisions", "budget_templates"):
        op.execute(
            f"ALTER TABLE {table} ADD FOREIGN KEY(agency_id,client_id,workflow_id) "
            "REFERENCES workflows(agency_id,client_id,id)"
        )
    op.execute(
        "CREATE UNIQUE INDEX credential_one_active_version "
        "ON credential_versions(credential_id) WHERE state='active'"
    )
    op.execute(
        "ALTER TABLE provider_calls ADD FOREIGN KEY(agency_id,credential_version_id) "
        "REFERENCES credential_versions(agency_id,id)"
    )
    from migrations.phase3_policy_backfill import backfill

    backfill(op.get_bind())


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM workflows) OR\n        "
        "EXISTS(SELECT 1 FROM administrative_events) THEN\n        RAISE "
        "EXCEPTION 'Phase 3 downgrade requires the upgrade backup';"
        "\n        END IF; END $$"
    )
    op.execute("ALTER TABLE workflows DROP COLUMN policy_version")
    op.execute("ALTER TABLE workflows DROP COLUMN policy_review_required")
    op.execute("ALTER TABLE provider_credentials DROP COLUMN active_version_id")
    op.execute("ALTER TABLE provider_credentials DROP COLUMN encryption_key_id")
    op.execute("ALTER TABLE budgets DROP COLUMN revision")
    op.execute("ALTER TABLE budgets DROP COLUMN template_revision_id")
    op.execute("ALTER TABLE runs DROP COLUMN stopped")
    op.execute("ALTER TABLE runs DROP COLUMN stop_reason")
    op.execute("ALTER TABLE runs DROP COLUMN stop_revision")
    op.execute("ALTER TABLE reservations DROP COLUMN client_budget_id")
    op.execute("ALTER TABLE approvals DROP COLUMN policy_version")
    op.execute("ALTER TABLE approvals DROP COLUMN matched_rules")
    op.execute("ALTER TABLE approvals DROP COLUMN invalidated_at")
    op.execute("ALTER TABLE approvals DROP COLUMN encryption_key_id")
    op.execute("ALTER TABLE provider_accounts DROP COLUMN validated_credential_version_id")
    op.execute("ALTER TABLE integration_destinations DROP COLUMN encryption_key_id")
    op.execute("ALTER TABLE provider_calls DROP COLUMN client_budget_id")
    op.execute("ALTER TABLE provider_calls DROP COLUMN policy_version")
    op.execute("ALTER TABLE provider_calls DROP COLUMN credential_version_id")
    op.execute("DROP TABLE deployment_keys")
    op.execute("DROP TABLE administrative_events")
    op.execute("DROP TABLE credential_versions")
    op.execute("DROP TABLE policy_revisions")
    op.execute("DROP TABLE budget_templates")
    op.execute("DROP TABLE client_budgets")
    op.execute("ALTER TABLE provider_credentials DROP CONSTRAINT credential_agency_identity")
    op.execute("DROP FUNCTION public.credential_version_guard()")
