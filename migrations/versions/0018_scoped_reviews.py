"""Client-scoped reviewers, linked identities, destinations, and price overrides."""

import base64
import json
import os
import uuid

import sqlalchemy as sa
from alembic import op
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

TABLES = [
    "reviewer_groups",
    "reviewer_group_members",
    "reviewer_assignments",
    "integration_destinations",
    "telegram_link_challenges",
    "agency_prices",
]


def upgrade() -> None:
    op.execute("ALTER TABLE workflows ADD UNIQUE (agency_id, client_id, id)")
    op.execute("""
CREATE TABLE reviewer_groups (
	id UUID NOT NULL, 
	agency_id UUID NOT NULL, 
	client_id UUID NOT NULL, 
	name TEXT NOT NULL, 
	active BOOLEAN DEFAULT 'true' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (agency_id, id), 
	UNIQUE (agency_id, client_id, id), 
	UNIQUE (client_id, name), 
	FOREIGN KEY(agency_id, client_id) REFERENCES clients (agency_id, id), 
	FOREIGN KEY(agency_id) REFERENCES agencies (id)
)

""")
    op.execute("""
CREATE TABLE reviewer_group_members (
	agency_id UUID NOT NULL, 
	group_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	PRIMARY KEY (agency_id, group_id, user_id), 
	FOREIGN KEY(agency_id, group_id) REFERENCES reviewer_groups (agency_id, id), 
	FOREIGN KEY(agency_id, user_id) REFERENCES agency_memberships (agency_id, user_id)
)

""")
    op.execute("""
CREATE TABLE reviewer_assignments (
	id UUID NOT NULL, 
	agency_id UUID NOT NULL, 
	client_id UUID NOT NULL, 
	group_id UUID NOT NULL, 
	workflow_id UUID, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agency_id, client_id, group_id) REFERENCES reviewer_groups
        (agency_id, client_id, id), 
	FOREIGN KEY(agency_id, client_id, workflow_id) REFERENCES workflows (agency_id, client_id, id)
)

""")
    op.execute(
        "CREATE UNIQUE INDEX reviewer_client_scope ON reviewer_assignments (group_id) WHERE "
        "workflow_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX reviewer_workflow_scope ON reviewer_assignments (group_id, "
        "workflow_id) WHERE workflow_id IS NOT NULL"
    )
    op.execute("""
CREATE TABLE integration_destinations (
	id UUID NOT NULL, 
	agency_id UUID NOT NULL, 
	client_id UUID, 
	workflow_id UUID, 
	group_id UUID, 
	name TEXT NOT NULL, 
	channel TEXT NOT NULL, 
	enabled BOOLEAN DEFAULT 'false' NOT NULL, 
	config JSONB DEFAULT '{}' NOT NULL, 
	legacy_source TEXT, 
	service_identity TEXT, 
	credential_hash BYTEA, 
	secret_ciphertext BYTEA, 
	secret_nonce BYTEA, 
	PRIMARY KEY (id), 
	UNIQUE (agency_id, id), 
	UNIQUE (agency_id, legacy_source), 
	FOREIGN KEY(agency_id, client_id) REFERENCES clients (agency_id, id), 
	FOREIGN KEY(agency_id, client_id, workflow_id) REFERENCES workflows (agency_id, client_id, id), 
	FOREIGN KEY(agency_id, client_id, group_id) REFERENCES reviewer_groups
        (agency_id, client_id, id), 
	CHECK (channel IN ('telegram','email','webhook')),
    CHECK ((workflow_id IS NULL AND group_id IS NULL) OR client_id IS NOT NULL), 
        CHECK (NOT enabled OR (client_id IS NOT NULL AND ((channel IN ('email','telegram') AND
        group_id IS NOT NULL) OR (channel='webhook' AND credential_hash IS NOT NULL AND
        secret_ciphertext IS NOT NULL AND secret_nonce IS NOT NULL AND service_identity IS NOT
        NULL)))),
	FOREIGN KEY(agency_id) REFERENCES agencies (id)
)

""")
    op.execute("""
CREATE TABLE telegram_link_challenges (
	token_hash BYTEA NOT NULL, 
	agency_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	consumed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(agency_id, user_id) REFERENCES agency_memberships (agency_id, user_id)
)

""")
    op.execute("""
CREATE TABLE agency_prices (
	agency_id UUID NOT NULL, 
	provider TEXT NOT NULL, 
	model TEXT NOT NULL, 
	effective_from DATE NOT NULL, 
	rates JSONB NOT NULL, 
	updated_by TEXT NOT NULL, 
	PRIMARY KEY (agency_id, provider, model, effective_from), 
	FOREIGN KEY(agency_id) REFERENCES agencies (id)
)

""")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY agency_isolation ON {table} "
            "USING (agency_id=NULLIF(current_setting('governflow.agency_id',true),'')::uuid) "
            "WITH CHECK (agency_id=NULLIF(current_setting('governflow.agency_id',true),'')::uuid)"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO governflow_runtime")
    op.execute("ALTER TABLE notifications DROP CONSTRAINT notifications_approval_id_channel_key")
    op.execute("ALTER TABLE notifications ADD COLUMN destination_id uuid")
    op.execute("ALTER TABLE notifications ADD UNIQUE (approval_id, destination_id)")
    op.execute(
        "ALTER TABLE notifications ADD FOREIGN KEY (agency_id,destination_id) REFERENCES "
        "integration_destinations(agency_id,id)"
    )
    op.execute("UPDATE notifications SET status='cancelled' WHERE status='pending'")
    # Preserve existing dashboard review grants as explicit groups, with no delivery destinations.
    op.execute("""INSERT INTO reviewer_groups (id,agency_id,client_id,name)
        SELECT gen_random_uuid(),agency_id,id,'Migrated reviewers' FROM clients""")
    op.execute("""INSERT INTO reviewer_assignments (id,agency_id,client_id,group_id)
        SELECT gen_random_uuid(),agency_id,client_id,id FROM reviewer_groups""")
    op.execute("""INSERT INTO reviewer_group_members (agency_id,group_id,user_id)
        SELECT g.agency_id,g.id,m.user_id FROM reviewer_groups g JOIN agency_memberships m
        ON m.agency_id=g.agency_id WHERE m.role IN ('admin','approver') AND
        (m.all_clients OR EXISTS (SELECT 1 FROM client_assignments a
            WHERE a.agency_id=g.agency_id AND a.user_id=m.user_id AND a.client_id=g.client_id))""")
    for name, channel in (
        ("TELEGRAM_APPROVER_CHAT_IDS", "telegram"),
        ("APPROVAL_EMAIL_TO", "email"),
        ("APPROVAL_WEBHOOK_URL", "webhook"),
    ):
        value = os.environ.get(name, "")
        if value:
            identity = uuid.uuid4()
            nonce = os.urandom(12)
            key = base64.b64decode(os.environ["MASTER_ENCRYPTION_KEY"], validate=True)
            ciphertext = AESGCM(key).encrypt(
                nonce,
                value.encode(),
                f"00000000-0000-0000-0000-000000000001:integration:{identity}".encode(),
            )
            config = json.dumps(
                {
                    "legacy_ciphertext": base64.b64encode(ciphertext).decode(),
                    "legacy_nonce": base64.b64encode(nonce).decode(),
                }
            )
            op.get_bind().execute(
                sa.text("""INSERT INTO integration_destinations
                (id,agency_id,name,channel,enabled,config,legacy_source)
                VALUES (:id,'00000000-0000-0000-0000-000000000001',:name,:channel,false,
                        CAST(:config AS jsonb),:name)"""),
                {"id": identity, "name": name, "channel": channel, "config": config},
            )
    op.execute("GRANT SELECT, INSERT, DELETE ON external_login_identities TO governflow_runtime")
    op.execute(
        "CREATE UNIQUE INDEX telegram_one_account ON external_login_identities(user_id) WHERE "
        "issuer='telegram'"
    )
    # Resolve context after verifying the credential, linking challenge, or signed callback.
    op.execute("""CREATE FUNCTION governflow_destination_agency(identity uuid, digest bytea)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT d.agency_id FROM public.integration_destinations d JOIN public.agencies a ON
        a.id=d.agency_id
        WHERE d.id=identity AND d.credential_hash=digest AND d.enabled AND d.channel='webhook'
        AND a.status='active' $$""")
    op.execute("""CREATE FUNCTION governflow_link_agency(digest bytea) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT agency_id FROM public.telegram_link_challenges WHERE token_hash=digest
        AND consumed_at IS NULL AND expires_at>now() $$""")
    op.execute("""CREATE FUNCTION governflow_approval_agency(identity uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT p.agency_id FROM public.approvals p JOIN public.agencies a ON a.id=p.agency_id
        WHERE p.id=identity AND a.status='active' $$""")
    for function in (
        "governflow_destination_agency(uuid,bytea)",
        "governflow_link_agency(bytea)",
        "governflow_approval_agency(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO governflow_runtime")


def downgrade() -> None:
    # Ownership migration guards prevent unsafe downgrade of a populated multi-agency release.
    for function in (
        "governflow_destination_agency(uuid,bytea)",
        "governflow_link_agency(bytea)",
        "governflow_approval_agency(uuid)",
    ):
        op.execute(f"DROP FUNCTION {function}")
    op.execute("DROP INDEX telegram_one_account")
    op.execute("ALTER TABLE notifications DROP COLUMN destination_id CASCADE")
    # Legacy uniqueness is restored only when multiple destinations remain representable.
    op.execute("ALTER TABLE notifications ADD UNIQUE (approval_id,channel)")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE workflows DROP CONSTRAINT workflows_agency_id_client_id_id_key")
