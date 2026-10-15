from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slidetwin import __version__
from slidetwin.api import main
from slidetwin.api.settings import Settings
from slidetwin.core.errors import ConfigError
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
            "SLIDETWIN_LOG_DIR": "/l",
            "SLIDETWIN_CACHE_DIR": "",
            "SLIDETWIN_MAX_SESSIONS": "3",
            "SLIDETWIN_SESSION_TTL_S": "60",
            "SLIDETWIN_SESSION_MAX_AGE_S": "600",
            "SLIDETWIN_MAX_BODY_BYTES": "4096",
        }
    )
    assert s.manifest == Path("/c/slides/manifest.yaml")
    assert s.slide_dir == Path("/s")
    assert s.log_dir == Path("/l")
    assert s.cache_dir is None
    assert (s.max_sessions, s.idle_ttl_s, s.max_age_s, s.max_body_bytes) == (3, 60.0, 600.0, 4096)
    d = Settings.from_env({})
    assert d.config_dir == Path("configs")
    assert d.log_dir == Path("logs")
    assert d.cache_dir == Path("cache")


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("SLIDETWIN_LOG_DIR", ""),
        ("SLIDETWIN_MAX_SESSIONS", "0"),
        ("SLIDETWIN_MAX_SESSIONS", "many"),
        ("SLIDETWIN_SESSION_TTL_S", "nan"),
        ("SLIDETWIN_SESSION_TTL_S", "-5"),
        ("SLIDETWIN_SESSION_MAX_AGE_S", "inf"),
        ("SLIDETWIN_MAX_BODY_BYTES", "-1"),
        ("SLIDETWIN_MAX_BODY_BYTES", "100"),
    ],
)
def test_bad_settings_fail_at_startup(var: str, value: str) -> None:
    with pytest.raises(ConfigError, match=var):
        Settings.from_env({var: value})


def test_unwritable_log_dir_fails_at_startup(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    settings = Settings.from_env(
        {"SLIDETWIN_CONFIG_DIR": str(CONFIG_DIR), "SLIDETWIN_LOG_DIR": str(blocker / "logs")}
    )
    with pytest.raises(ConfigError, match="not writable"):
        main.create_app(settings)


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
