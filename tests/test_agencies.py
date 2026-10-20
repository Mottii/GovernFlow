"""Agency-bound sessions, multi-client grants, and hostile resource selectors."""

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from admin_api.app import app
from core.models import (
    Agency,
    AgencyMembership,
    Client,
    ClientAssignment,
    User,
)

PASSWORD = "agency acceptance password"


async def login(http, email, agency_id=None):
    body = {"email": email, "password": PASSWORD}
    if agency_id:
        body["agency_id"] = str(agency_id)
    response = await http.post("/admin/auth/login", json=body)
    assert response.status_code == 200, response.text
    return {"authorization": "Bearer " + response.json()["token"]}


@pytest.mark.asyncio
async def test_agency_isolation_multi_client_assignments_and_membership_revocation(governed):
    master = {"authorization": "Bearer test-admin"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin"
    ) as http:
        # The default agency keeps both existing client identifiers and the original login API.
        created = await http.post(
            "/admin/users",
            headers=master,
            json={
                "email": "owner@example.test",
                "password": PASSWORD,
                "role": "admin",
            },
        )
        assert created.status_code == 201
        owner_id = created.json()["id"]
        default = await login(http, "owner@example.test")
        default_id = (await http.get("/admin/auth/me", headers=default)).json()["agency_id"]
        other = await http.post("/admin/agencies", headers=master, json={"name": "Agency B"})
        assert other.status_code == 201
        agency_id = other.json()["id"]
        assert (
            await http.post("/admin/agencies", headers=default, json={"name": "Escalation"})
        ).status_code == 403
        assert (
            await http.put(
                f"/admin/agencies/{agency_id}/memberships/{owner_id}",
                headers=master,
                json={"role": "admin", "all_clients": True},
            )
        ).status_code == 200
        # Agency choice is verified against the authenticated user's current membership.
        ambiguous = await http.post(
            "/admin/auth/login",
            json={
                "email": "owner@example.test",
                "password": PASSWORD,
            },
        )
        assert ambiguous.status_code == 409
        second = await login(http, "owner@example.test", agency_id)
        clients = []
        workflows = []
        for name in ("B1", "B2"):
            client = await http.post("/admin/clients", headers=second, json={"name": name})
            assert client.status_code == 201
            clients.append(client.json()["id"])
            workflow = await http.post(
                "/admin/workflows",
                headers=second,
                json={
                    "client_id": clients[-1],
                    "name": name,
                    "risk_class": "R1",
                },
            )
            assert workflow.status_code == 201
            workflows.append(workflow.json()["id"])
        assert {r["id"] for r in (await http.get("/admin/clients", headers=second)).json()} == set(
            clients
        )
        assert {r["id"] for r in (await http.get("/admin/clients", headers=default)).json()} == {
            str(governed.a.client.id),
            str(governed.b.client.id),
        }
        # An untrusted header or query cannot override a session's agency.
        forged = {**second, "x-agency-id": default_id}
        for route in (
            "clients",
            "workflows",
            "spend",
            "keys",
            "approvals",
            "alerts",
            "kill",
            "reconciliation",
            "users",
        ):
            response = await http.get(
                "/admin/" + route, headers=forged, params={"agency_id": default_id}
            )
            assert response.status_code == 200, (route, response.text)
            assert str(governed.a.client.id) not in response.text
            assert str(governed.a.workflow.id) not in response.text
        for headers, client_id, workflow_id in (
            (second, str(governed.a.client.id), str(governed.a.workflow.id)),
            (default, clients[0], workflows[0]),
        ):
            for route in ("workflows", "spend"):
                assert (
                    await http.get(
                        "/admin/" + route, headers=headers, params={"client_id": client_id}
                    )
                ).status_code == 404
            for method, route, body in (
                (
                    "POST",
                    "/admin/workflows",
                    {"client_id": client_id, "name": "foreign", "risk_class": "R1"},
                ),
                ("POST", "/admin/keys", {"workflow_id": workflow_id}),
                (
                    "POST",
                    "/admin/budgets",
                    {"workflow_id": workflow_id, "period": "2099-01", "cap_micro": 10},
                ),
                (
                    "POST",
                    "/admin/provider-credentials",
                    {"client_id": client_id, "provider": "openai", "key": "fixture"},
                ),
                (
                    "POST",
                    "/admin/provider-accounts",
                    {
                        "client_id": client_id,
                        "provider": "openai",
                        "external_id": "fixture",
                        "validate_live": False,
                    },
                ),
                ("POST", "/admin/kill", {"scope": "client", "scope_id": client_id, "active": True}),
                (
                    "POST",
                    "/admin/kill",
                    {"scope": "workflow", "scope_id": workflow_id, "active": True},
                ),
            ):
                result = await http.request(method, route, headers=headers, json=body)
                assert result.status_code == 404, (route, result.text)
            for report_format in ("json", "csv", "html"):
                assert (
                    await http.get(
                        f"/admin/reports/{client_id}/2026-10",
                        headers=headers,
                        params={"format": report_format},
                    )
                ).status_code == 404
            assert (await http.post("/admin/prices/seed", headers=headers)).status_code == 403
            assert (
                await http.post(
                    "/admin/kill", headers=headers, json={"scope": "global", "active": True}
                )
            ).status_code == 403
        staff = await http.post(
            "/admin/users",
            headers=second,
            json={
                "email": "staff@example.test",
                "password": PASSWORD,
                "role": "operator",
                "client_ids": clients,
            },
        )
        assert staff.status_code == 201
        staff_id = staff.json()["id"]
        staff_headers = await login(http, "staff@example.test")
        assert {
            r["id"] for r in (await http.get("/admin/clients", headers=staff_headers)).json()
        } == set(clients)
        # Unauthorized agency selection fails even with a valid password.
        assert (
            await http.post(
                "/admin/auth/login",
                json={
                    "email": "staff@example.test",
                    "password": PASSWORD,
                    "agency_id": default_id,
                },
            )
        ).status_code == 401
        # Changing grants takes effect on the next request, including already-issued sessions.
        assert (
            await http.put(
                f"/admin/memberships/{staff_id}",
                headers=second,
                json={
                    "role": "operator",
                    "client_ids": clients[:1],
                },
            )
        ).status_code == 200
        assert [
            r["id"] for r in (await http.get("/admin/clients", headers=staff_headers)).json()
        ] == clients[:1]
        assert (
            await http.post(
                "/admin/keys", headers=staff_headers, json={"workflow_id": workflows[1]}
            )
        ).status_code == 404
        assert (
            await http.put(
                f"/admin/memberships/{staff_id}",
                headers=second,
                json={
                    "role": "operator",
                    "client_ids": [str(governed.a.client.id)],
                },
            )
        ).status_code == 404
        # Disabling membership in one agency does not disable the user's other membership.
        assert (await http.delete(f"/admin/users/{staff_id}", headers=default)).status_code == 404
        assert (await http.delete(f"/admin/users/{staff_id}", headers=second)).status_code == 204
        assert (await http.get("/admin/clients", headers=staff_headers)).status_code == 401
        assert (await http.get("/admin/clients", headers=default)).status_code == 200
        assert (
            await http.put(
                f"/admin/memberships/{owner_id}",
                headers=second,
                json={
                    "role": "viewer",
                    "all_clients": True,
                },
            )
        ).status_code == 409
        # Agency suspension invalidates existing sessions immediately.
        async with governed.factory() as session:
            await session.execute(
                update(Agency).where(Agency.id == agency_id).values(status="suspended")
            )
            await session.commit()
        assert (await http.get("/admin/clients", headers=second)).status_code == 401
        assert (await http.get("/admin/clients", headers=default)).status_code == 200


@pytest.mark.asyncio
async def test_assignment_database_constraint_rejects_foreign_agency_client(governed):
    async with governed.factory() as session:
        agency = Agency(name="Foreign")
        user = User(
            email="constraint@example.test",
            role="viewer",
            password_hash=b"fixture",
            password_salt=b"fixture",
        )
        session.add_all([agency, user])
        await session.flush()
        session.add(AgencyMembership(agency_id=agency.id, user_id=user.id, role="viewer"))
        await session.flush()
        session.add(
            ClientAssignment(agency_id=agency.id, user_id=user.id, client_id=governed.a.client.id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_virtual_key_context_tracks_agency_and_suspension(governed):
    from core.models import VirtualKey, Workflow
    from core.security import virtual_key_hash
    from gateway.auth import resolve_virtual_key
    from gateway.errors import GatewayError

    async with governed.factory() as session:
        agency = Agency(name="Moved fixture")
        session.add(agency)
        await session.flush()
        client = Client(agency_id=agency.id, name="New agency client")
        session.add(client)
        await session.flush()
        workflow = Workflow(client_id=client.id, name="isolated", risk_class="R1", policy={})
        session.add(workflow)
        await session.flush()
        key = "gk_new_agency_key"
        session.add(
            VirtualKey(workflow_id=workflow.id, key_hash=virtual_key_hash(key), prefix=key[:8])
        )
        await session.commit()
        auth = await resolve_virtual_key(session, key)
        assert auth.agency_id == agency.id and auth.client_id == client.id
        assert auth.workflow_id == workflow.id
        await session.execute(
            update(Agency).where(Agency.id == agency.id).values(status="suspended")
        )
        await session.commit()
        with pytest.raises(GatewayError) as error:
            await resolve_virtual_key(session, key)
        assert error.value.status_code == 401
    async with governed.factory() as session:
        assert (
            await resolve_virtual_key(session, governed.a.key)
        ).client_id == governed.a.client.id
