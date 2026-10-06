from fastapi.testclient import TestClient

from admin_api.app import app as admin_app
from gateway.app import app as gateway_app


def test_gateway_health() -> None:
    assert TestClient(gateway_app).get("/health").json() == {"status": "ok"}


def test_admin_health() -> None:
    assert TestClient(admin_app).get("/health").json() == {"status": "ok"}
