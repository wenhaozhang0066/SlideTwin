"""Pilot feedback (M7): stored next to the session log, never in the trajectory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slidetwin.core.config import ConfigStore
from slidetwin.core.errors import SessionNotFound
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from tests.api.conftest import open_session
from tests.conftest import CONFIG_DIR


def test_feedback_is_saved_beside_the_log(client: TestClient, settings: object) -> None:
    sid = open_session(client)["session_id"]
    url = f"/api/sessions/{sid}/feedback"
    resp = client.post(url, json={"navigation": 5, "timing": 3, "comment": "fine"})
    assert resp.status_code == 200
    assert resp.json() == {"saved": True}
    path = Path(settings.log_dir) / "feedback" / f"{sid}.json"  # type: ignore[attr-defined]
    saved = json.loads(path.read_text())
    assert saved["session_id"] == sid
    assert (saved["navigation"], saved["timing"], saved["report"]) == (5, 3, None)
    assert saved["received_utc"].endswith("+00:00")
    trajectory = client.get(f"/api/sessions/{sid}/trajectory").text
    assert "fine" not in trajectory
    assert client.post(url, json={"report": 1}).status_code == 200  # a later answer replaces it
    assert json.loads(path.read_text())["report"] == 1


def test_feedback_is_validated(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    url = f"/api/sessions/{sid}/feedback"
    for body in [
        {"navigation": 6},
        {"timing": True},
        {"timing": "3"},
        {"mood": 3},
        {"comment": "x" * 4001},
    ]:
        assert client.post(url, json=body).status_code == 422, body
    resp = client.post(f"/api/sessions/{'0' * 32}/feedback", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


def test_feedback_after_the_session_ended(tmp_path: Path, manifest: Path, slide_dir: Path) -> None:
    registry = SlideRegistry.from_manifest(manifest, slide_dir)
    mgr = SessionManager(registry=registry, configs=ConfigStore(CONFIG_DIR), log_dir=tmp_path)
    s = mgr.create(slide_id="syn40", actor="agent:x")
    mgr.close_all()
    mgr.save_feedback(s.session_id, {"comment": "late"})
    assert (
        json.loads((tmp_path / "feedback" / f"{s.session_id}.json").read_text())["comment"]
        == "late"
    )
    bare = SessionManager(registry=registry, configs=ConfigStore(CONFIG_DIR))
    live = bare.create(slide_id="syn40", actor="agent:y")
    for sid in [live.session_id, "not-an-id"]:  # no log folder; not a session id
        with pytest.raises(SessionNotFound):
            bare.save_feedback(sid, {})
    bare.close_all()
    registry.close()
