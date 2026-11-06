"""Health endpoints without a database (connection refused on port 1)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__
from app.main import create_app
from tests.conftest import make_unit_settings


def test_liveness_ok_without_database() -> None:
    with TestClient(create_app(make_unit_settings())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["environment"] == "test"


def test_readiness_reports_503_when_database_unreachable() -> None:
    with TestClient(create_app(make_unit_settings())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["database"]["status"] == "error"
    assert body["checks"]["migrations"]["detail"] == "database unavailable"
    # Error details must not leak connection strings / credentials.
    assert "unit-test-password" not in response.text


def test_request_id_is_generated_and_echoed() -> None:
    with TestClient(create_app(make_unit_settings())) as client:
        generated = client.get("/health")
        echoed = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert len(generated.headers["X-Request-ID"]) == 32
    assert echoed.headers["X-Request-ID"] == "abc-123"


def test_oversized_request_id_is_replaced() -> None:
    with TestClient(create_app(make_unit_settings())) as client:
        response = client.get("/health", headers={"X-Request-ID": "x" * 500})
    assert response.headers["X-Request-ID"] != "x" * 500
