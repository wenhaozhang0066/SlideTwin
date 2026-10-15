"""Regression tests for the confirmed findings of the M2 adversarial review (ids in brackets)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
import yaml
from fastapi.testclient import TestClient

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.trajectory import parse_trajectory
from tests.api.conftest import ClientFactory, open_session
from tests.core.test_report import EXAMPLE


def url(sid: str, tail: str) -> str:
    return f"/api/sessions/{sid}/{tail}"


def err(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def events(client: TestClient, sid: str) -> list[dict[str, Any]]:
    _, evs = parse_trajectory(client.get(url(sid, "trajectory")).text)
    return evs


def test_huge_integer_in_geometry_is_a_logged_400(client: TestClient) -> None:
    """[M2-C1] Client input never yields an unlogged 500."""
    sid = open_session(client)["session_id"]
    body = '{"geometry": {"type": "point", "coordinates": [1' + "0" * 400 + ', 5]}, "label": "x"}'
    resp = client.post(
        url(sid, "annotations"), content=body, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400
    assert err(resp)["code"] == "INVALID_PARAMETER"
    assert events(client, sid)[1]["action"] == "annotate"


@pytest.mark.parametrize(
    ("tail", "body"),
    [
        ("pan", {"dx": True, "dy": 0}),
        ("magnification", {"mag": "10"}),
        ("move_to", {"x_um": "1e2", "y_um": 0}),
        ("measure", {"p1_px": [True, 0], "p2_px": [1, 1]}),
    ],
)
def test_requests_are_not_coerced(client: TestClient, tail: str, body: dict[str, Any]) -> None:
    """[M2-C2] Booleans and numeric strings are refused, exactly as in the core."""
    sid = open_session(client)["session_id"]
    resp = client.post(url(sid, tail), json=body)
    assert resp.status_code == 422
    assert err(resp)["code"] == "INVALID_REQUEST"


def test_viewport_booleans_are_refused(client: TestClient) -> None:
    """[M2-C2]"""
    resp = client.post(
        "/api/sessions",
        json={"slide_id": "syn40", "actor": "agent:t", "viewport_px": [True, 768]},
    )
    assert resp.status_code == 422


def test_report_schema_is_served(client: TestClient) -> None:
    """[M2-C7] An API-only client can learn the report structure."""
    schema = client.get("/api/report_schema").json()
    assert schema["title"] == "SlideTwin report"
    assert "Conclusion" in schema["$defs"]
    sid = open_session(client)["session_id"]
    resp = client.post(url(sid, "report"), json={"report": {"task": "sln_metastasis"}})
    assert "/api/report_schema" in err(resp)["hint"]


def test_undecodable_body_is_invalid_request(client: TestClient) -> None:
    """[M2-C5] Not 400 HTTP_ERROR."""
    sid = open_session(client)["session_id"]
    resp = client.post(
        url(sid, "annotations"),
        content=b'{"label": "5 \xb5m"}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert err(resp)["code"] == "INVALID_REQUEST"


def test_405_names_the_allowed_method(client: TestClient) -> None:
    """[M2-C6] RFC 9110 requires Allow; the message says what to use instead."""
    sid = open_session(client)["session_id"]
    resp = client.get(url(sid, "pan"))
    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST"
    assert "use POST" in err(resp)["message"]
    resp = client.get("/api/nowhere")
    assert err(resp)["hint"] == "See the API at /docs."


def test_openapi_documents_server_errors(client: TestClient) -> None:
    """[M2-C11] 500 is documented on every action route; state documents only what it returns."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "500" in paths["/api/sessions/{session_id}/pan"]["post"]["responses"]
    state = paths["/api/sessions/{session_id}/state"]["get"]["responses"]
    assert "409" not in state
    assert "404" in state


def test_catalogue_has_no_source_urls(client: TestClient) -> None:
    """[M2-SEC-01] File names in URLs can carry labels."""
    for slide in client.get("/api/slides").json()["slides"]:
        assert set(slide) == {"id", "license", "attribution"}


def test_line_separators_through_rest_keep_the_log_parseable(client: TestClient) -> None:
    """[M2-SEC-02]"""
    sid = open_session(client)["session_id"]
    client.post(
        url(sid, "annotations"),
        json={
            "geometry": {"type": "point", "coordinates": [1, 1]},
            "label": "x",
            "note": "a\u2028b\u2029c\x85d",
        },
    )
    assert events(client, sid)[1]["params"]["note"] == "a\u2028b\u2029c\x85d"


def test_rejected_oversized_fields_are_bounded_in_the_log(client: TestClient) -> None:
    """[M2-SEC-03, R2] A refused 150 KB label does not add 150 KB to the trajectory."""
    sid = open_session(client)["session_id"]
    resp = client.post(
        url(sid, "annotations"),
        json={"geometry": {"type": "point", "coordinates": [1, 1]}, "label": "x" * 150_000},
    )
    assert resp.status_code == 400
    assert len(client.get(url(sid, "trajectory")).text) < 20_000


def test_slide_errors_name_the_id_not_the_file(make_client: ClientFactory, tmp_path: Path) -> None:
    """[M2-SEC-04]"""
    slides = tmp_path / "slides"
    slides.mkdir()
    tifffile.imwrite(slides / "tumor_042.tif", np.zeros((256, 256, 3), np.uint8), tile=(256, 256))
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({"slides": [{"id": "s42", "path": "tumor_042.tif"}]}))
    from tests.conftest import CONFIG_DIR

    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=manifest,
        slide_dir=slides,
        log_dir=tmp_path / "logs",
        cache_dir=None,
    )
    with TestClient(create_app(settings)) as client:
        resp = client.post("/api/sessions", json={"slide_id": "s42", "actor": "agent:t"})
    assert resp.status_code == 422
    assert "s42" in err(resp)["message"]
    assert "tumor" not in resp.text


def test_deeply_nested_input_is_refused_and_logged(client: TestClient) -> None:
    """[M2-SEC-05] No seq gap, no 500."""
    sid = open_session(client)["session_id"]
    nested: Any = [1, 1]
    for _ in range(500):
        nested = [nested]
    resp = client.post(
        url(sid, "annotations"),
        json={"geometry": {"type": "point", "coordinates": nested}, "label": "x"},
    )
    assert resp.status_code == 400
    evs = events(client, sid)
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))


def test_invalid_creations_do_not_touch_slides_or_disk(
    make_client: ClientFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[R4, R6] Bad actor/viewport are refused before any slide is opened or folder created."""
    client = make_client()
    mgr = client.app.state.manager  # type: ignore[attr-defined]
    calls: list[str] = []
    real_get = mgr.registry.get
    monkeypatch.setattr(mgr.registry, "get", lambda sid: calls.append(sid) or real_get(sid))
    for body in (
        {"slide_id": "syn40", "actor": "Dr. Who"},
        {"slide_id": "syn40", "actor": "agent:t", "viewport_px": [5, 5]},
        {"slide_id": "nope", "actor": "agent:t"},
    ):
        assert client.post("/api/sessions", json=body).status_code in (400, 404)
    assert calls == []
    sessions_dir = settings.log_dir / "sessions"
    assert not sessions_dir.exists() or not any(p.is_dir() for p in sessions_dir.iterdir())


def test_ended_sessions_lose_images_but_keep_trajectories(make_client: ClientFactory) -> None:
    """[R6] Images of an ended session are deleted; its trajectory stays downloadable."""
    now = [0.0]
    client = make_client(idle_ttl_s=60.0, clock=lambda: now[0])
    data = open_session(client)
    sid = data["session_id"]
    mgr = client.app.state.manager  # type: ignore[attr-defined]
    image_dir = mgr.log_dir / "sessions" / sid
    assert image_dir.is_dir()
    now[0] = 61.0
    mgr.sweep()
    assert not image_dir.exists()
    assert json.loads(client.get(url(sid, "trajectory")).text.splitlines()[0])["session_id"] == sid


def test_downloads_do_not_keep_a_session_alive(make_client: ClientFactory) -> None:
    """[R8] Only actions refresh the idle timer; sessions also have an absolute lifetime."""
    now = [0.0]
    client = make_client(idle_ttl_s=60.0, max_age_s=100.0, clock=lambda: now[0])
    sid = open_session(client)["session_id"]
    for t in (30.0, 59.0):
        now[0] = t
        assert client.get(url(sid, "views/v_0001.jpg")).status_code == 200
    now[0] = 61.0
    assert client.get(url(sid, "state")).status_code == 404

    sid = open_session(client)["session_id"]  # created at t=61
    now[0] = 100.0
    assert client.get(url(sid, "state")).status_code == 200  # reading the state...
    now[0] = 125.0
    assert client.get(url(sid, "state")).status_code == 404  # ...does not keep it alive

    sid = open_session(client)["session_id"]  # created at t=125
    for t in (170.0, 220.0):
        now[0] = t
        assert client.post(url(sid, "view")).status_code == 200  # actions keep it alive...
    now[0] = 226.0
    assert client.get(url(sid, "state")).status_code == 404  # ...but not past its lifetime


def test_too_many_sessions_says_when_to_retry(make_client: ClientFactory) -> None:
    """[R8]"""
    client = make_client(max_sessions=1)
    open_session(client)
    resp = client.post("/api/sessions", json={"slide_id": "syn40", "actor": "agent:t"})
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "60"


def test_closed_session_report_flow_still_downloads(client: TestClient) -> None:
    """[M2-C9] With the required log dir, a submitted session's trajectory survives eviction."""
    sid = open_session(client)["session_id"]
    assert client.post(url(sid, "report"), json={"report": EXAMPLE}).status_code == 200
    mgr = client.app.state.manager  # type: ignore[attr-defined]
    mgr.close_all()
    assert client.get(url(sid, "trajectory")).status_code == 200
