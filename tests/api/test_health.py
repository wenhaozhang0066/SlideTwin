from __future__ import annotations

from fastapi.testclient import TestClient

from slidetwin import __version__
from slidetwin.api.main import create_app


def test_health_reports_version_and_sha() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["git_sha"]


def test_openapi_is_served() -> None:
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
