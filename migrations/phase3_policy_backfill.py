"""Frozen legacy policy conversion for 0020."""

import json

from sqlalchemy import text

from migrations.phase3_policy_v1 import digest, normalize


def backfill(connection):
    rows = list(
        connection.execute(
            text("SELECT id,agency_id,client_id,risk_class,policy FROM workflows")
        ).mappings()
    )
    for row in rows:
        invalid = False
        try:
            policy = normalize(row["policy"], row["risk_class"], legacy=True)
        except (ValueError, TypeError):
            invalid = True
            policy = normalize({"models": [], "rules": []}, row["risk_class"])
        params = {
            **row,
            "legacy": json.dumps(row["policy"]),
            "policy": json.dumps(policy),
            "hash": digest(policy, row["risk_class"]),
            "invalid": invalid,
        }
        connection.execute(
            text(
                "INSERT INTO policy_revisions(id,agency_id,client_id,workflow_id,"
                "version,risk_class,policy,content_hash,actor,legacy_source) VALU"
                "ES(gen_random_uuid(),:agency_id,:client_id,:id,1,:risk_class,CAS"
                "T(:policy AS jsonb),:hash,'migration',CAST(:legacy AS jsonb))"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE workflows SET policy=CAST(:policy AS "
                "jsonb),policy_version=1,policy_review_required=:invalid WHERE "
                "id=:id"
            ),
            params,
        )
    connection.execute(
        text(
            "UPDATE approvals a SET policy_version=w.policy_version FROM "
            "workflows w WHERE w.id=a.workflow_id"
        )
    )
