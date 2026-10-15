from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slidetwin import __version__
from slidetwin.api import main
from slidetwin.api.settings import Settings
from tests.conftest import CONFIG_DIR


def test_health_reports_version_and_sha(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["git_sha"]


def test_openapi_and_docs_are_served(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {
            "SLIDETWIN_CONFIG_DIR": "/c",
            "SLIDETWIN_SLIDE_DIR": "/s",
            "SLIDETWIN_LOG_DIR": "",
            "SLIDETWIN_CACHE_DIR": "/k",
            "SLIDETWIN_MAX_SESSIONS": "3",
            "SLIDETWIN_SESSION_TTL_S": "60",
            "SLIDETWIN_MAX_BODY_BYTES": "1000",
        }
    )
    assert s.manifest == Path("/c/slides/manifest.yaml")
    assert s.slide_dir == Path("/s")
    assert s.log_dir is None
    assert s.cache_dir == Path("/k")
    assert (s.max_sessions, s.idle_ttl_s, s.max_body_bytes) == (3, 60.0, 1000)
    d = Settings.from_env({})
    assert d.config_dir == Path("configs")
    assert d.log_dir == Path("logs")


def test_module_app_is_built_lazily(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLIDETWIN_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("SLIDETWIN_SLIDE_DIR", str(tmp_path))
    monkeypatch.setenv("SLIDETWIN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(main, "_app", None)
    app = main.app
    assert isinstance(app, FastAPI)
    assert main.app is app
    with pytest.raises(AttributeError):
        _ = main.not_there
