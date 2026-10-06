from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from admin_api.app import app
from core.models import AdminSession, Approval, User, Workflow


@pytest.mark.asyncio
async def test_admin_roles_all_routes_tenant_scope_and_revocable_login(governed):
    master = {"authorization": "Bearer test-admin"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin"
    ) as admin:
        credentials = {}
        for role in ("operator", "viewer", "approver"):
            email = f"{role}@example.test"
            user = await admin.post(
                "/admin/users",
                headers=master,
                json={
                    "email": email,
                    "password": "acceptance password 123",
                    "role": role,
                    "client_id": str(governed.a.client.id),
                },
            )
            assert user.status_code == 201
            result = await admin.post(
                "/admin/auth/login", json={"email": email, "password": "acceptance password 123"}
            )
            assert result.status_code == 200 and "no-store" in result.headers["cache-control"]
            credentials[role] = {"authorization": "Bearer " + result.json()["token"]}
        operator = credentials["operator"]
        viewer = credentials["viewer"]
        approver = credentials["approver"]
        assert (
            await admin.post(
                "/admin/auth/login", json={"email": "missing@example.test", "password": "bad"}
            )
        ).status_code == 401
        assert (await admin.get("/admin/auth/me", headers=viewer)).json()["role"] == "viewer"
        for route in (
            "clients",
            "workflows",
            "spend",
            "keys",
            "approvals",
            "alerts",
            "kill",
            "reconciliation",
        ):
            response = await admin.get("/admin/" + route, headers=viewer)
            assert response.status_code == 200, (route, response.text)
            assert (
                str(governed.b.client.id) not in response.text
                and str(governed.b.workflow.id) not in response.text
            )
            assert "password_hash" not in response.text and "key_hash" not in response.text
            assert (
                await admin.get(
                    "/admin/" + route, headers={"authorization": "Bearer " + governed.a.key}
                )
            ).status_code == 401
        for route in ("workflows", "spend"):
            assert (
                await admin.get(
                    "/admin/" + route,
                    params={"client_id": str(governed.b.client.id)},
                    headers=viewer,
                )
            ).status_code == 404
        assert (
            await admin.get(
                "/admin/keys", params={"workflow_id": str(governed.b.workflow.id)}, headers=operator
            )
        ).status_code == 404
        other_key = (
            await admin.post(
                "/admin/keys", headers=master, json={"workflow_id": str(governed.b.workflow.id)}
            )
        ).json()
        async with governed.factory() as session:
            await session.execute(
                update(Workflow).values(risk_class="R2", policy={"actions": {"publish": {}}})
            )
            await session.commit()
        approvals = {}
        for tenant in (governed.a, governed.b):
            result = await governed.http.post(
                "/v1/actions/check",
                headers={"authorization": "Bearer " + tenant.key},
                json={
                    "action_name": "publish",
                    "action": {"text": "content"},
                    "summary": "Review content",
                },
            )
            approvals[tenant.client.name] = result.json()["approval_id"]
        foreign = approvals["B"]
        foreign_requests = [
            (
                "POST",
                "/admin/workflows",
                {"client_id": str(governed.b.client.id), "name": "foreign", "risk_class": "R1"},
            ),
            (
                "POST",
                "/admin/budgets",
                {"workflow_id": str(governed.b.workflow.id), "period": "2099-01", "cap_micro": 1},
            ),
            (
                "POST",
                "/admin/provider-credentials",
                {"client_id": str(governed.b.client.id), "provider": "openai", "key": "x"},
            ),
            (
                "POST",
                "/admin/provider-accounts",
                {
                    "client_id": str(governed.b.client.id),
                    "provider": "openai",
                    "external_id": "foreign",
                    "validate_live": False,
                },
            ),
            ("POST", "/admin/keys", {"workflow_id": str(governed.b.workflow.id)}),
            ("DELETE", "/admin/keys/" + other_key["id"], None),
            (
                "POST",
                "/admin/kill",
                {"scope": "workflow", "scope_id": str(governed.b.workflow.id), "active": True},
            ),
            (
                "POST",
                "/admin/kill",
                {"scope": "client", "scope_id": str(governed.b.client.id), "active": True},
            ),
        ]
        for method, route, body in foreign_requests:
            response = await admin.request(method, route, json=body, headers=operator)
            assert response.status_code == 404, (route, response.text)
        for route in (f"/admin/actions/{foreign}", f"/admin/actions/{foreign}/decision"):
            response = await admin.request(
                "POST" if route.endswith("decision") else "GET",
                route,
                json={"decision": "approve", "approver": "fake"}
                if route.endswith("decision")
                else None,
                headers=approver,
            )
            assert response.status_code == 404
        for role in (viewer, operator, approver):
            assert (
                await admin.post("/admin/clients", headers=role, json={"name": "forbidden"})
            ).status_code == 403
            assert (await admin.post("/admin/prices/seed", headers=role)).status_code == 403
            assert (await admin.get("/admin/users", headers=role)).status_code == 403
            assert (
                await admin.post(
                    "/admin/users",
                    headers=role,
                    json={
                        "email": "x@example.test",
                        "password": "acceptance password",
                        "role": "admin",
                    },
                )
            ).status_code == 403
            assert (
                await admin.delete("/admin/users/" + str(governed.b.client.id), headers=role)
            ).status_code == 403
            assert (
                await admin.post(
                    "/admin/kill", headers=role, json={"scope": "global", "active": True}
                )
            ).status_code == 403
        assert (
            await admin.post(
                "/admin/keys", headers=viewer, json={"workflow_id": str(governed.a.workflow.id)}
            )
        ).status_code == 403
        assert (
            await admin.post(
                "/admin/workflows",
                headers=operator,
                json={
                    "client_id": str(governed.a.client.id),
                    "name": "failopen",
                    "risk_class": "R1",
                    "policy": {"outage": {"mode": "open"}},
                },
            )
        ).status_code == 403
        own = approvals["A"]
        assert (
            await admin.post(
                f"/admin/actions/{own}/decision",
                headers=operator,
                json={"decision": "approve", "approver": "fake"},
            )
        ).status_code == 403
        assert (await admin.get(f"/admin/actions/{own}", headers=approver)).json()["action"][
            "text"
        ] == "content"
        assert (
            await admin.post(
                f"/admin/actions/{own}/decision",
                headers=approver,
                json={"decision": "approve", "approver": "fake"},
            )
        ).status_code == 200
        async with governed.factory() as session:
            record = await session.get(Approval, own)
            assert record.approver == "approver@example.test"
            stored = (await session.execute(select(User))).scalars().all()
            assert all(b"acceptance password" not in u.password_hash for u in stored)
            await session.execute(
                update(AdminSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        assert (await admin.get("/admin/spend", headers=viewer)).status_code == 401
        logged = await admin.post(
            "/admin/auth/login",
            json={"email": "viewer@example.test", "password": "acceptance password 123"},
        )
        viewer = {"authorization": "Bearer " + logged.json()["token"]}
        assert (await admin.post("/admin/auth/logout", headers=viewer)).status_code == 204
        assert (await admin.get("/admin/spend", headers=viewer)).status_code == 401
