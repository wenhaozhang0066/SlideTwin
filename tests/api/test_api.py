"""REST contract tests (SPEC 7): every action, its errors and the information-closure rules."""

from __future__ import annotations

import base64
import io
import json
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from slidetwin.api.settings import Settings
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.replay import replay
from slidetwin.core.session import Session
from tests.api.conftest import SECRET_ANNOTATION, SECRET_LABEL, ClientFactory, open_session
from tests.conftest import CONFIG_DIR
from tests.core.test_report import EXAMPLE

ACTION_ROUTES = {
    ("post", "/api/sessions"): "create_session",
    ("get", "/api/sessions/{session_id}/state"): "get_state",
    ("post", "/api/sessions/{session_id}/thumbnail"): "get_thumbnail",
    ("post", "/api/sessions/{session_id}/magnification"): "set_magnification",
    ("post", "/api/sessions/{session_id}/pan"): "pan",
    ("post", "/api/sessions/{session_id}/move_to"): "move_to",
    ("post", "/api/sessions/{session_id}/view"): "view",
    ("post", "/api/sessions/{session_id}/measure"): "measure",
    ("post", "/api/sessions/{session_id}/annotations"): "annotate",
    ("post", "/api/sessions/{session_id}/report"): "submit_report",
    ("get", "/api/sessions/{session_id}/trajectory"): "export_trajectory",
    ("post", "/api/sessions/{session_id}/crop"): "crop",
}


def url(sid: str, tail: str) -> str:
    return f"/api/sessions/{sid}/{tail}"


def err(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def decode(obs: dict[str, Any]) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(obs["image"]["base64"])))


# --- the API surface ---------------------------------------------------------------------------


def test_every_spec_action_has_exactly_one_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    found = {(m, p) for p, ops in paths.items() for m in ops}
    for route in ACTION_ROUTES:
        assert route in found, route
    assert not [p for p in paths if "tile" in p.lower() or "dzi" in p.lower()]
    image_routes = [p for p in paths if p.endswith(".jpg")]
    assert sorted(image_routes) == [
        "/api/sessions/{session_id}/thumbnails/{thumb_id}.jpg",
        "/api/sessions/{session_id}/views/{view_id}.jpg",
    ]


def test_catalogue_has_no_labels(client: TestClient) -> None:
    slides = client.get("/api/slides").json()["slides"]
    assert [s["id"] for s in slides] == ["syn20", "syn40"]
    assert slides[1]["license"] == "CC0-1.0"
    profiles = client.get("/api/profiles").json()["profiles"]
    assert {p["id"] for p in profiles} == {"viewer", "unconstrained"}
    protocols = client.get("/api/protocols").json()["protocols"]
    assert protocols[0]["protocol_id"] == "sln_metastasis"


# --- session creation --------------------------------------------------------------------------


def test_create_session(client: TestClient) -> None:
    data = open_session(client)
    sid = data["session_id"]
    assert len(sid) == 32
    assert data["action"] == "create_session"
    assert data["seq"] == 1
    assert data["cost_s"] == 0.3
    obs = data["observation"]
    assert obs["type"] == "view"
    assert obs["view_id"] == "v_0001"
    assert decode(obs).size == (512, 384)
    assert obs["image"]["url"] == f"/api/sessions/{sid}/views/v_0001.jpg"
    assert client.get(obs["image"]["url"]).content == base64.b64decode(obs["image"]["base64"])
    assert data["slide"]["mpp"] == [0.25, 0.25]
    assert data["slide"]["dimensions_um"] == [512.0, 384.0]
    assert data["slide"]["max_mag"] == 40.0
    assert data["task"]["protocol_id"] == "sln_metastasis"
    assert data["task"]["status"] == "draft"
    assert data["rules"]["id"] == "viewer"
    assert data["state"]["mag"] == 1.25


def test_default_viewport_is_the_profile_default(client: TestClient) -> None:
    data = open_session(client, viewport_px=None)
    assert data["state"]["viewport_px"] == data["rules"]["viewport_default_px"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"slide_id": "nope"}, 404, "SLIDE_NOT_FOUND"),
        ({"profile": "microscope"}, 400, "INVALID_PARAMETER"),
        ({"protocol": "t9"}, 400, "INVALID_PARAMETER"),
        ({"actor": "Dr. Smith"}, 400, "INVALID_PARAMETER"),
        ({"viewport_px": [10, 10]}, 400, "INVALID_PARAMETER"),
        ({"viewport_px": [10]}, 422, "INVALID_REQUEST"),
        ({"unknown": 1}, 422, "INVALID_REQUEST"),
        ({"actor": None}, 422, "INVALID_REQUEST"),
    ],
)
def test_create_errors(client: TestClient, body: dict[str, Any], status: int, code: str) -> None:
    payload = {"slide_id": "syn40", "actor": "agent:test", "viewport_px": [512, 384], **body}
    resp = client.post("/api/sessions", json=payload)
    assert resp.status_code == status, resp.text
    assert err(resp)["code"] == code
    assert err(resp)["message"]


# --- actions -----------------------------------------------------------------------------------


def test_navigation_actions(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    r = client.post(url(sid, "magnification"), json={"mag": 10}).json()
    assert (r["action"], r["cost_s"], r["state"]["mag"]) == ("set_magnification", 0.8, 10.0)
    r = client.post(url(sid, "magnification"), json={"mag": 40}).json()
    r = client.post(url(sid, "pan"), json={"dx": 0.5, "dy": 0}).json()
    assert r["cost_s"] == 0.575  # SPEC appendix B
    assert r["observation"]["view_id"] == "v_0004"
    r = client.post(url(sid, "move_to"), json={"x_um": 100.5, "y_um": 200.25}).json()
    assert r["state"]["center_um"] == [100.5, 200.25]
    assert r["cost_s"] == 0.9
    r = client.post(url(sid, "view")).json()
    assert r["cost_s"] == 0.3
    r = client.post(url(sid, "view"), json={"client": {"think_ms": 5}}).json()
    assert r["seq"] == 7
    r = client.post(url(sid, "pan"), params={"include_image": "false"}, json={"dx": 0, "dy": 0.1})
    obs = r.json()["observation"]
    assert obs["image"]["base64"] is None
    assert client.get(obs["image"]["url"]).headers["content-type"] == "image/jpeg"
    state = client.get(url(sid, "state")).json()
    assert state["observation"] is None
    assert state["cost_s"] == 0.0
    assert state["state"]["views_served"] == 8


def test_thumbnail_measure_annotate(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    t = client.post(url(sid, "thumbnail")).json()
    assert t["cost_s"] == 0.5
    obs = t["observation"]
    assert obs["thumb_id"] == "t_0001"
    assert decode(obs).size == (1024, 768)
    assert obs["um_per_px"] == [0.5, 0.5]
    assert client.get(obs["image"]["url"]).status_code == 200
    client.post(url(sid, "magnification"), json={"mag": 40})
    m = client.post(url(sid, "measure"), json={"p1_px": [0, 0], "p2_px": [240, 320]}).json()
    assert m["observation"]["length_um"] == 100.0  # 400 px at 0.25 um/px
    assert m["cost_s"] == 3.0
    a = client.post(
        url(sid, "annotations"),
        json={"geometry": {"type": "point", "coordinates": [10, 20]}, "label": "x"},
    ).json()
    assert a["observation"]["annotation_id"] == "a_0001"
    bad = client.post(url(sid, "measure"), json={"p1_px": [0, 0], "p2_px": [9999, 0]})
    assert bad.status_code == 400
    assert err(bad)["code"] == "OUTSIDE_VIEW"


def test_spec_error_message_for_unavailable_magnification(client: TestClient) -> None:
    sid = open_session(client, slide_id="syn20")["session_id"]
    resp = client.post(url(sid, "magnification"), json={"mag": 40})
    assert resp.status_code == 400
    assert err(resp) == {
        "code": "MAG_UNAVAILABLE",
        "message": "40x is not available: this slide's native resolution is 0.50 um/px, "
        "so the highest magnification is 20x.",
        "hint": "Use one of: 1.25x, 2.5x, 5x, 10x, 20x.",
    }
    resp = client.post(url(sid, "magnification"), json={"mag": 3})
    assert err(resp)["code"] == "MAG_NOT_PRESET"


@pytest.mark.parametrize(
    ("tail", "body"),
    [
        ("pan", {"dx": "NaN", "dy": 0}),
        ("pan", {"dx": 1}),
        ("pan", {"dx": 1, "dy": 0, "extra": 1}),
        ("move_to", {"x_um": [1], "y_um": 0}),
        ("measure", {"p1_px": [0], "p2_px": [1, 1]}),
        ("magnification", {}),
    ],
)
def test_request_validation_uses_the_error_envelope(
    client: TestClient, tail: str, body: dict[str, Any]
) -> None:
    sid = open_session(client)["session_id"]
    resp = client.post(url(sid, tail), json=body)
    assert resp.status_code == 422
    assert err(resp)["code"] == "INVALID_REQUEST"
    assert err(resp)["details"]


def test_literal_nan_in_json_is_rejected(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    resp = client.post(
        url(sid, "pan"),
        content=b'{"dx": NaN, "dy": 0}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422


def test_crop_only_under_unconstrained(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    resp = client.post(url(sid, "crop"), json={"x_um": 0, "y_um": 0, "w_um": 100, "h_um": 100})
    assert resp.status_code == 403
    assert err(resp)["code"] == "ACTION_NOT_ALLOWED"
    sid = open_session(client, profile="unconstrained")["session_id"]
    r = client.post(url(sid, "crop"), json={"x_um": 10, "y_um": 20, "w_um": 256, "h_um": 128})
    assert r.status_code == 200
    obs = r.json()["observation"]
    assert obs["type"] == "crop"
    assert obs["size_px"] == [512, 256]
    assert obs["mpp"] == 0.5
    assert client.get(obs["image"]["url"]).status_code == 200


def test_report_closes_the_session(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    bad = client.post(url(sid, "report"), json={"report": {"task": "sln_metastasis"}})
    assert bad.status_code == 422
    assert err(bad)["code"] == "REPORT_INVALID"
    ok = client.post(url(sid, "report"), json={"report": EXAMPLE})
    assert ok.status_code == 200
    receipt = ok.json()["observation"]
    assert receipt["type"] == "report_receipt"
    assert receipt["views_used"] == ["v_0001"]
    closed = client.post(url(sid, "pan"), json={"dx": 0.1, "dy": 0})
    assert closed.status_code == 409
    assert err(closed)["code"] == "SESSION_CLOSED"
    assert client.get(url(sid, "state")).json()["state"]["closed"] is True
    assert client.get(url(sid, "trajectory")).status_code == 200


# --- trajectory and replay ------------------------------------------------------------------------


def test_trajectory_download_replays(client: TestClient, settings: Settings) -> None:
    sid = open_session(client)["session_id"]
    client.post(url(sid, "thumbnail"))
    client.post(url(sid, "magnification"), json={"mag": 20})
    client.post(url(sid, "pan"), json={"dx": 0.3, "dy": -0.2, "client": {"think_ms": 900}})
    client.post(url(sid, "magnification"), json={"mag": 7})  # logged failure
    client.post(url(sid, "measure"), json={"p1_px": [1.5, 2.5], "p2_px": [100, 50]})
    resp = client.get(url(sid, "trajectory"))
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert sid in resp.headers["content-disposition"]
    lines = [json.loads(x) for x in resp.text.splitlines()]
    assert lines[0]["type"] == "header"
    assert [e["action"] for e in lines[1:]] == [
        "create_session", "get_thumbnail", "set_magnification", "pan",
        "set_magnification", "measure", "export_trajectory",
    ]  # fmt: skip
    assert lines[4]["client"] == {"think_ms": 900}
    assert lines[5]["error"]["code"] == "MAG_NOT_PRESET"
    on_disk = (settings.log_dir or Path()) / "sessions" / f"{sid}.jsonl"
    assert on_disk.read_text() == resp.text
    registry = SlideRegistry.from_manifest(settings.manifest, settings.slide_dir)
    report = replay(resp.text, registry)
    assert report.ok, report.mismatches


def test_rest_serpentine_scan_end_to_end(client: TestClient, settings: Settings) -> None:
    """A minimal serpentine screen at 10x driven only through REST, then report and replay."""
    data = open_session(client, viewport_px=[256, 192])
    sid = data["session_id"]
    w_um, h_um = data["slide"]["dimensions_um"]
    client.post(url(sid, "magnification"), json={"mag": 10})  # fov 256 x 192 um
    cols, rows = int(w_um // 256), int(h_um // 192)
    seen = set()
    for row in range(rows):
        order = range(cols) if row % 2 == 0 else reversed(range(cols))
        for col in order:
            x, y = 128 + col * 256, 96 + row * 192
            r = client.post(url(sid, "move_to"), json={"x_um": x, "y_um": y})
            assert r.status_code == 200
            seen.add(tuple(r.json()["observation"]["bbox_um"]))
    assert len(seen) == cols * rows
    negative = {**EXAMPLE, "conclusion": {**EXAMPLE["conclusion"], "metastasis": "absent"}}
    negative["conclusion"]["category"] = "none"
    negative["claims"] = []
    assert client.post(url(sid, "report"), json={"report": negative}).status_code == 200
    text = client.get(url(sid, "trajectory")).text
    registry = SlideRegistry.from_manifest(settings.manifest, settings.slide_dir)
    assert replay(text, registry).ok


# --- information closure ------------------------------------------------------------


def test_images_are_scoped_to_their_session(client: TestClient) -> None:
    a = open_session(client)["session_id"]
    b = open_session(client)["session_id"]
    client.post(url(a, "magnification"), json={"mag": 10})  # a now has v_0002; b has only v_0001
    resp = client.get(url(b, "views/v_0002.jpg"))
    assert resp.status_code == 404
    assert err(resp)["code"] == "IMAGE_NOT_FOUND"
    assert client.get(url(b, "thumbnails/t_0001.jpg")).status_code == 404
    assert client.get(url(b, "views/x_0001.jpg")).status_code == 422
    unknown = client.get(url("0" * 32, "views/v_0001.jpg"))
    assert unknown.status_code == 404
    assert err(unknown)["code"] == "SESSION_NOT_FOUND"
    assert client.get(url("not-a-session", "state")).status_code == 422


def test_ground_truth_never_leaves_the_server(client: TestClient, settings: Settings) -> None:
    bodies = [
        client.get("/api/slides").text,
        client.get("/api/profiles").text,
        client.get("/api/protocols").text,
        client.get("/openapi.json").text,
    ]
    created = client.post(
        "/api/sessions", json={"slide_id": "syn40", "actor": "agent:t", "viewport_px": [256, 192]}
    )
    bodies.append(created.text)
    sid = created.json()["session_id"]
    for tail, body in [
        ("thumbnail", {}),
        ("magnification", {"mag": 10}),
        ("pan", {"dx": 1, "dy": 1}),
        ("report", {"report": EXAMPLE}),
    ]:
        bodies.append(client.post(url(sid, tail), json=body).text)
    bodies.append(client.get(url(sid, "trajectory")).text)
    for path in (settings.log_dir or Path()).rglob("*.jsonl"):
        bodies.append(path.read_text())
    for text in bodies:
        assert SECRET_LABEL not in text
        assert SECRET_ANNOTATION not in text
        assert "eval_only" not in text


# --- limits ------------------------------------------------------------------------------------


def budget_configs(tmp_path: Path, **budget: Any) -> Path:
    target = tmp_path / "cfg"
    shutil.copytree(CONFIG_DIR, target)
    path = target / "profiles" / "viewer.yaml"
    data = yaml.safe_load(path.read_text())
    data["budget"] = {"max_virtual_s": None, "max_views": None, **budget}
    path.write_text(yaml.safe_dump(data))
    return target


def test_budget_exhaustion(make_client: ClientFactory, tmp_path: Path) -> None:
    client = make_client(config_dir=budget_configs(tmp_path, max_views=2))
    sid = open_session(client)["session_id"]
    assert client.post(url(sid, "magnification"), json={"mag": 10}).status_code == 200
    resp = client.post(url(sid, "pan"), json={"dx": 0.1, "dy": 0})
    assert resp.status_code == 409
    assert err(resp)["code"] == "BUDGET_EXHAUSTED"
    assert client.post(url(sid, "report"), json={"report": EXAMPLE}).status_code == 200


def test_session_limit_and_eviction_of_closed_sessions(make_client: ClientFactory) -> None:
    client = make_client(max_sessions=1)
    sid = open_session(client)["session_id"]
    resp = client.post(
        "/api/sessions", json={"slide_id": "syn40", "actor": "agent:t", "viewport_px": [256, 192]}
    )
    assert resp.status_code == 503
    assert err(resp)["code"] == "TOO_MANY_SESSIONS"
    client.post(url(sid, "report"), json={"report": EXAMPLE})
    assert open_session(client)["session_id"] != sid  # the closed session made room


def test_idle_sessions_expire_but_logs_stay(make_client: ClientFactory) -> None:
    now = [0.0]
    client = make_client(idle_ttl_s=60.0, clock=lambda: now[0])
    sid = open_session(client)["session_id"]
    now[0] = 61.0
    resp = client.get(url(sid, "state"))
    assert resp.status_code == 404
    assert err(resp)["code"] == "SESSION_NOT_FOUND"
    text = client.get(url(sid, "trajectory")).text
    assert json.loads(text.splitlines()[0])["session_id"] == sid
    assert client.get(url("f" * 32, "trajectory")).status_code == 404


def test_oversized_bodies_are_refused(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    report = {**EXAMPLE, "free_text": "x" * 300_000}
    resp = client.post(url(sid, "report"), json={"report": report})
    assert resp.status_code == 413
    assert err(resp)["code"] == "PAYLOAD_TOO_LARGE"
    assert client.get(url(sid, "state")).json()["state"]["closed"] is False


def test_chunked_oversized_body_is_refused(client: TestClient) -> None:
    sid = open_session(client)["session_id"]

    def chunks() -> Any:
        for _ in range(30):
            yield b"x" * 10_000

    resp = client.post(
        url(sid, "report"), content=chunks(), headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413


def test_internal_errors_do_not_leak_details(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from slidetwin.api.main import create_app

    client = TestClient(create_app(settings), raise_server_exceptions=False)
    sid = open_session(client)["session_id"]

    def boom(self: Session, **kw: Any) -> None:
        raise RuntimeError("/secret/path/to/slide.svs exploded")

    monkeypatch.setattr(Session, "view", boom)
    resp = client.post(url(sid, "view"))
    assert resp.status_code == 500
    assert err(resp) == {
        "code": "INTERNAL",
        "message": "Internal error. The failed action was logged.",
    }
    assert "secret" not in resp.text


def test_unknown_routes_use_the_envelope(client: TestClient) -> None:
    resp = client.get("/api/tiles/0/0/0.jpg")
    assert resp.status_code == 404
    assert err(resp)["code"] == "NOT_FOUND"
    resp = client.delete("/api/slides")
    assert resp.status_code == 405
    assert err(resp)["code"] == "METHOD_NOT_ALLOWED"


def test_concurrent_actions_on_one_session(client: TestClient) -> None:
    sid = open_session(client)["session_id"]
    client.post(url(sid, "magnification"), json={"mag": 10})
    errors: list[Any] = []

    def worker() -> None:
        for _ in range(5):
            r = client.post(
                url(sid, "pan"), json={"dx": 0.01, "dy": 0}, params={"include_image": "false"}
            )
            if r.status_code != 200:
                errors.append(r.text)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    lines = [json.loads(x) for x in client.get(url(sid, "trajectory")).text.splitlines()]
    assert [e["seq"] for e in lines[1:]] == list(range(1, 2 + 1 + 20 + 1))


@pytest.mark.testdata
def test_cmu_slide_through_the_api(cmu_small_path: Path, tmp_path: Path) -> None:
    from slidetwin.api.main import create_app

    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=CONFIG_DIR / "slides" / "manifest.yaml",
        slide_dir=cmu_small_path.parent.parent,
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
    )
    with TestClient(create_app(settings)) as client:
        data = open_session(client, slide_id="cmu1_small", viewport_px=[1024, 768])
        sid = data["session_id"]
        assert data["slide"]["available_mags"] == [1.25, 2.5, 5.0, 10.0, 20.0]
        resp = client.post(url(sid, "magnification"), json={"mag": 40})
        assert err(resp)["message"] == (
            "40x is not available: this slide's native resolution is 0.50 um/px, "
            "so the highest magnification is 20x."
        )
