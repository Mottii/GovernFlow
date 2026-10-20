import csv
import io
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from admin_api.app import app
from core.models import Event, Run, Workflow


@pytest.mark.asyncio
async def test_monthly_exports_equal_database_totals_and_scope_savings(governed):
    master = {"authorization": "Bearer test-admin"}
    period = datetime.now(UTC).strftime("%Y-%m")
    async with governed.factory() as session:
        await session.execute(
            update(Workflow)
            .where(Workflow.id == governed.a.workflow.id)
            .values(name="=SUM(A1) <script>alert(1)</script>")
        )
        await session.commit()
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://provider.test/v1/chat/completions").respond(
            200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        )
        for tenant in (governed.a, governed.a, governed.b):
            assert (
                await governed.http.post(
                    "/v1/chat/completions",
                    headers={"authorization": "Bearer " + tenant.key},
                    json={"model": "gpt-mock", "max_tokens": 16},
                )
            ).status_code == 200
    path = f"/admin/reports/{governed.a.client.id}/{period}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin"
    ) as admin:
        async with governed.factory() as session:
            await session.execute(update(Run).values(stopped=True, stop_reason="report-test"))
            await session.commit()
        for tenant in (governed.a, governed.b):
            template = await admin.post(
                "/admin/budget-templates",
                headers=master,
                json={
                    "scope": "client",
                    "scope_id": str(tenant.client.id),
                    "cap_micro": 100000,
                    "expected_revision": 0,
                    "reason": "Report renewal",
                },
            )
            assert template.status_code == 201, template.text
        report = (await admin.get(path, headers=master)).json()
        assert report["client_budget"]["spent_micro"] == 30
        assert [t["scope_id"] for t in report["renewal_templates"]] == [str(governed.a.client.id)]
        assert {r["workflow_id"] for r in report["run_stops"]} == {str(governed.a.workflow.id)}
        assert all(r["reason"] == "report-test" for r in report["run_stops"])
        spend = (
            await admin.get(
                "/admin/spend", headers=master, params={"client_id": str(governed.a.client.id)}
            )
        ).json()
        assert spend["run_stops"] == report["run_stops"]
        assert spend["renewal_templates"] == report["renewal_templates"]
        assert report["hours_saved"] is None
        result = await admin.post(
            path + "/hours",
            headers=master,
            json={"minutes_saved": 123, "basis": "estimate", "note": "=A1 <script>bad()</script>"},
        )
        assert result.status_code == 200 and result.json()["hours"] == "2.050000"
        report = (await admin.get(path, headers=master)).json()
        async with governed.factory() as session:
            actual = (
                await session.execute(
                    select(func.sum(Event.cost_micro)).where(
                        Event.client_id == governed.a.client.id, Event.type == "provider_call"
                    )
                )
            ).scalar_one()
        assert report["totals"]["cost_micro"] == actual == 30
        assert report["totals"]["calls"] == 2
        assert report["hours_saved"]["minutes"] == 123
        exported = await admin.get(path, params={"format": "csv"}, headers=master)
        assert (
            exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
        )
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        assert rows[0]["workflow"].startswith("'=SUM")
        assert (
            int(rows[-1]["cost_micro_usd"])
            == sum(int(row["cost_micro_usd"]) for row in rows[:-1])
            == actual
        )
        assert rows[-1]["cost_usd"] == "0.000030" and rows[-1]["minutes_saved"] == "123"
        assert rows[-1]["savings_note"].startswith("'=A1")
        printed = await admin.get(path, params={"format": "html"}, headers=master)
        assert "<script>" not in printed.text and "&lt;script&gt;" in printed.text
        assert "$0.000030" in printed.text and "2.050000 hours (estimate)" in printed.text
        assert "default-src 'none'" in printed.headers["content-security-policy"]
        assert (
            await admin.get(f"/admin/reports/{governed.a.client.id}/2099-01", headers=master)
        ).json()["totals"]["cost_micro"] == 0
        for invalid in ("0000-01", "2026-13", "2026-1", "9999-12"):
            assert (
                await admin.get(f"/admin/reports/{governed.a.client.id}/{invalid}", headers=master)
            ).status_code == 400
        for role in ("operator", "viewer"):
            await admin.post(
                "/admin/users",
                headers=master,
                json={
                    "email": role + "@report.test",
                    "password": "report acceptance password",
                    "role": role,
                    "client_id": str(governed.a.client.id),
                },
            )
            login = (
                await admin.post(
                    "/admin/auth/login",
                    json={"email": role + "@report.test", "password": "report acceptance password"},
                )
            ).json()
            auth = {"authorization": "Bearer " + login["token"]}
            other = f"/admin/reports/{governed.b.client.id}/{period}"
            for format in ("json", "csv", "html"):
                assert (
                    await admin.get(other, params={"format": format}, headers=auth)
                ).status_code == 404
                own = await admin.get(path, params={"format": format}, headers=auth)
                assert own.status_code == 200 and str(governed.b.client.id) not in own.text
            assert (
                await admin.post(
                    other + "/hours", headers=auth, json={"minutes_saved": 1, "basis": "measured"}
                )
            ).status_code == (404 if role == "operator" else 403)
            assert (
                await admin.post(
                    path + "/hours", headers=auth, json={"minutes_saved": 1, "basis": "measured"}
                )
            ).status_code == (200 if role == "operator" else 403)
        assert (
            await admin.get(path, headers={"authorization": "Bearer " + governed.a.key})
        ).status_code == 401
