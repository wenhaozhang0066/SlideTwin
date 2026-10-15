"""Session state machine: actions, costs, clamping, failures and logging (SPEC 4, 7, 8, 9)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from slidetwin.core.config import Budget, Profile, ThumbnailRules
from slidetwin.core.errors import (
    ActionNotAllowed,
    BudgetExhausted,
    ImageNotFound,
    InvalidParameter,
    MagnificationNotPreset,
    MagnificationUnavailable,
    NoCurrentView,
    OutsideView,
    ReportInvalid,
    SessionClosed,
    SlideMetadataError,
)
from slidetwin.core.session import MeasurementRecord, Session, ThumbnailRecord, ViewRecord
from slidetwin.core.trajectory import parse_trajectory
from tests.conftest import SessionFactory
from tests.core.test_report import EXAMPLE
from tests.helpers.synth import dark_centroid, line_source

# Synthetic slide: 2048 x 1536 um at 1.0 um/px (native 10x); lines at x = 300, 1000, 1501 px.


def events(s: Session) -> list[dict[str, Any]]:
    return [r for r in s.history if r["type"] == "event"]


def test_creation(make_session: SessionFactory) -> None:
    s = make_session()
    st = s.state_dict()
    assert st["mag"] == 1.25
    assert st["center_um"] == [1024.0, 768.0]  # field of view wider than the slide: centred
    assert st["elapsed_s"] == 0.3  # initial view costs one render
    assert st["views_served"] == 1
    assert st["current_view_id"] == "v_0001"
    assert s.initial.observation is not None
    assert s.initial.image is not None
    assert s.available_mags == (1.25, 2.5, 5.0, 10.0)
    info = s.slide_info()
    assert info["dimensions_um"] == [2048.0, 1536.0]
    assert info["max_mag"] == 10.0
    assert s.task()["protocol_id"] == "sln_metastasis"
    header, evs = parse_trajectory(s.export_trajectory().text or "")
    assert header["viewport_px"] == [1024, 768]
    assert header["slide"]["backend"] == "array"
    assert header["profile"]["content"]["id"] == "viewer"
    assert evs[0]["action"] == "create_session"
    assert evs[0]["params"]["actor"] == "agent:test"


@pytest.mark.parametrize("actor", ["bob", "human:", "robot:x", "agent:" + "a" * 65, "human:a b", 3])
def test_actor_must_be_pseudonymous_and_typed(make_session: SessionFactory, actor: Any) -> None:
    with pytest.raises(InvalidParameter):
        make_session(actor=actor)


@pytest.mark.parametrize("vp", [(10, 10), (1024,), (1024, 5000), (True, 768), (1024.0, 768)])
def test_viewport_validation(make_session: SessionFactory, vp: Any) -> None:
    with pytest.raises(InvalidParameter):
        make_session(viewport_px=vp)


def test_default_viewport_comes_from_profile(make_session: SessionFactory, viewer: Profile) -> None:
    s = make_session(viewport_px=None)
    assert s.viewport_px == viewer.viewport_default_px


def test_no_available_magnification(make_session: SessionFactory) -> None:
    coarse = line_source(width=64, height=64, mpp=20.0, n_levels=1, xs=(), ys=())
    with pytest.raises(SlideMetadataError, match="No preset magnification"):
        make_session(source=coarse)


# --- magnification --------------------------------------------------------------------------


def test_set_magnification(make_session: SessionFactory) -> None:
    s = make_session()
    r = s.set_magnification(10)
    assert r.cost_s == 0.8
    assert r.elapsed_s == pytest.approx(1.1)
    assert r.state["mag"] == 10.0
    assert r.state["fov_um"] == [1024.0, 768.0]
    assert isinstance(r.observation, ViewRecord)
    assert r.observation.view_id == "v_0002"
    assert r.observation.bbox_um.to_list() == [512.0, 384.0, 1536.0, 1152.0]


def test_magnification_errors_are_specific_and_change_nothing(make_session: SessionFactory) -> None:
    s = make_session()
    before = s.state_dict()
    with pytest.raises(MagnificationUnavailable) as err:
        s.set_magnification(20)
    assert err.value.message == (
        "20x is not available: this slide's native resolution is 1.00 um/px, "
        "so the highest magnification is 10x."
    )
    assert err.value.hint == "Use one of: 1.25x, 2.5x, 5x, 10x."
    with pytest.raises(MagnificationNotPreset):
        s.set_magnification(7)
    with pytest.raises(InvalidParameter):
        s.set_magnification("10")
    assert s.state_dict() == before
    failed = events(s)[-3:]
    assert [e["error"]["code"] for e in failed] == [
        "MAG_UNAVAILABLE",
        "MAG_NOT_PRESET",
        "INVALID_PARAMETER",
    ]
    assert all(e["cost_s"] == 0 for e in failed)
    assert [e["seq"] for e in failed] == [2, 3, 4]


@pytest.mark.testdata
def test_cmu_slide_refuses_40x(make_session: SessionFactory, cmu_small_path: Path) -> None:
    from slidetwin.core.slide_source import open_slide

    s = make_session(source=open_slide(cmu_small_path))
    with pytest.raises(MagnificationUnavailable) as err:
        s.set_magnification(40)
    assert "native resolution is 0.50 um/px" in err.value.message
    assert "highest magnification is 20x" in err.value.message
    assert s.set_magnification(20).state["mag"] == 20.0


# --- navigation -----------------------------------------------------------------------------


def test_pan_costs_and_clamping(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    r = s.pan(0.5, 0)
    assert r.cost_s == 0.575  # SPEC appendix B
    assert r.state["center_um"] == [1536.0, 768.0]
    r = s.pan(0.5, 0)  # already at the right edge: no movement, base cost only
    assert r.state["center_um"] == [1536.0, 768.0]
    assert r.cost_s == 0.5
    r = s.pan(-0.25, 0.5)
    assert r.state["center_um"] == [1280.0, 1152.0]
    assert r.cost_s == pytest.approx(0.2 + 0.15 * (0.25**2 + 0.5**2) ** 0.5 + 0.3, abs=1e-6)
    bbox = r.state["bbox_um"]
    assert bbox[2] <= 2048.0
    assert bbox[3] <= 1536.0


@pytest.mark.parametrize(("dx", "dy"), [(0.3, 0.2), (-0.4, 0.1), (0.05, -0.45)])
def test_pan_is_reversible_away_from_edges(
    make_session: SessionFactory, dx: float, dy: float
) -> None:
    s = make_session()
    s.set_magnification(10)
    start = s.state_dict()["center_um"]
    s.pan(dx, dy)
    end = s.pan(-dx, -dy).state["center_um"]
    assert end == pytest.approx(start)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "1", True])
def test_pan_rejects_bad_numbers(make_session: SessionFactory, bad: Any) -> None:
    s = make_session()
    with pytest.raises(InvalidParameter):
        s.pan(bad, 0)


def test_move_to(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    r = s.move_to(0, 0)
    assert r.cost_s == 0.9
    assert r.state["center_um"] == [512.0, 384.0]
    assert s.move_to(650.5, 400).state["center_um"] == [650.5, 400.0]
    with pytest.raises(InvalidParameter):
        s.move_to(float("nan"), 0)


def test_view_refetch(make_session: SessionFactory) -> None:
    s = make_session()
    first = s.initial.observation
    r = s.view()
    assert r.cost_s == 0.3
    assert isinstance(r.observation, ViewRecord)
    assert isinstance(first, ViewRecord)
    assert r.observation.view_id == "v_0002"
    assert r.observation.pixel_sha256 == first.pixel_sha256
    assert r.observation.bbox_um == first.bbox_um


def test_served_images_are_jpeg_and_only_served_ids_are_available(
    make_session: SessionFactory,
) -> None:
    s = make_session()
    jpeg = s.get_image("v_0001")
    assert Image.open(io.BytesIO(jpeg)).size == (1024, 768)
    for bad in ("v_0002", "t_0001", "../x", "v_1"):
        with pytest.raises(ImageNotFound):
            s.get_image(bad)


# --- thumbnail and crop ----------------------------------------------------------------------


def test_thumbnail(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    r = s.get_thumbnail()
    assert r.cost_s == 0.5
    assert isinstance(r.observation, ThumbnailRecord)
    assert r.observation.thumb_id == "t_0001"
    assert r.observation.size_px == (1024, 768)
    assert r.observation.um_per_px == (2.0, 2.0)
    assert r.state["current_view_id"] == "v_0002"  # the thumbnail is not a view
    assert r.state["views_served"] == 2
    assert s.get_image("t_0001") == r.image


def test_thumbnail_can_be_forbidden(make_session: SessionFactory, viewer: Profile) -> None:
    prof = viewer.model_copy(update={"thumbnail": ThumbnailRules(allowed=False)})
    s = make_session(profile=prof)
    with pytest.raises(ActionNotAllowed):
        s.get_thumbnail()


def test_crop_only_in_unconstrained(make_session: SessionFactory, unconstrained: Profile) -> None:
    viewer_session = make_session()
    with pytest.raises(ActionNotAllowed, match="'viewer' profile"):
        viewer_session.crop(0, 0, 100, 100)
    s = make_session(profile=unconstrained)
    assert s.state_dict()["elapsed_s"] == 0.0  # flat model: initial view free
    assert s.get_thumbnail().cost_s == 0.0
    r = s.crop(100, 200, 1536, 768)  # long side scaled to the viewport's 1024 px
    assert r.cost_s == 1.0
    obs = r.observation
    assert isinstance(obs, ViewRecord)
    assert obs.kind == "crop"
    assert obs.size_px == (1024, 512)
    assert obs.mpp == 1.5
    assert obs.mag == pytest.approx(10 / 1.5)
    assert obs.bbox_um.to_list() == [100.0, 200.0, 1636.0, 968.0]
    same = s.crop(100, 200, 512, 256).observation  # would need 0.5 um/px: capped at native 1.0
    assert isinstance(same, ViewRecord)
    assert (same.size_px, same.mpp) == ((512, 256), 1.0)
    assert r.state["current_view_id"] == obs.view_id
    assert r.state["mag"] == 1.25  # crops do not move the viewer
    tiny = s.crop(10, 10, 20, 20).observation
    assert isinstance(tiny, ViewRecord)
    assert tiny.mpp == 1.0  # never finer than the scan
    assert tiny.size_px == (20, 20)


@pytest.mark.parametrize(
    "args", [(0, 0, 0, 10), (0, 0, 10, -5), (5000, 5000, 10, 10), (0, 0, "a", 1)]
)
def test_crop_errors(
    make_session: SessionFactory, unconstrained: Profile, args: tuple[Any, ...]
) -> None:
    s = make_session(profile=unconstrained)
    with pytest.raises(InvalidParameter):
        s.crop(*args)


# --- measure and annotate --------------------------------------------------------------------


def test_measure_is_physical(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    s.move_to(650.5, 400)  # bbox x0 = 138.5 um; lines at 300.5 and 1000.5 um
    r = s.measure([162, 300], [862, 300])
    assert r.cost_s == 3.0
    m = r.observation
    assert isinstance(m, MeasurementRecord)
    assert m.measurement_id == "m_0001"
    assert m.view_id == "v_0003"
    assert m.p1_um == (300.5, 316.0)
    assert m.p2_um == (1000.5, 316.0)
    assert m.length_um == 700.0
    assert m.length_px == 700.0
    assert s.measurements["m_0001"] == m


def test_rendered_pixels_agree_with_measurement_geometry(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    r = s.move_to(650.5, 400)
    img = Image.open(io.BytesIO(r.image or b"")).convert("L")
    import numpy as np

    row = np.asarray(img)[300]
    c1 = dark_centroid(row[100:220]) + 100
    c2 = dark_centroid(row[800:920]) + 800
    view = r.observation
    assert isinstance(view, ViewRecord)
    # pixel index k has its centre at k + 0.5 in view coordinates
    x1 = view.bbox_um.x0 + (c1 + 0.5) * view.mpp
    x2 = view.bbox_um.x0 + (c2 + 0.5) * view.mpp
    assert x1 == pytest.approx(300.5, abs=0.25)
    assert x2 == pytest.approx(1000.5, abs=0.25)


def test_measure_errors(make_session: SessionFactory) -> None:
    s = make_session()
    with pytest.raises(OutsideView, match=r"outside the current view \(1024x768 px\)"):
        s.measure([0, 0], [1025, 0])
    for bad in ([1, 2, 3], "1,2", [1, "a"], None):
        with pytest.raises(InvalidParameter):
            s.measure(bad, [0, 0])
    assert s.measure([0, 0], [1024, 768]).observation is not None  # edges are inside


def test_annotate(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    s.move_to(650.5, 400)
    r = s.annotate({"type": "point", "coordinates": [162, 300]}, "suspicious", "check at 20x")
    assert r.cost_s == 4.0
    a = s.annotations["a_0001"]
    assert a.coords_um == ((300.5, 316.0),)
    poly = s.annotate({"type": "polygon", "coordinates": [[0, 0], [10, 0], [10, 10]]}, "region")
    assert poly.observation is not None
    assert s.annotations["a_0002"].geometry_type == "polygon"


@pytest.mark.parametrize(
    ("geometry", "label", "note", "error"),
    [
        ({"type": "point", "coordinates": [2000, 0]}, "x", "", OutsideView),
        ({"type": "line", "coordinates": [[0, 0], [1, 1]]}, "x", "", InvalidParameter),
        ({"type": "polygon", "coordinates": [[0, 0], [1, 1]]}, "x", "", InvalidParameter),
        ({"type": "polygon", "coordinates": "abc"}, "x", "", InvalidParameter),
        ([1, 2], "x", "", InvalidParameter),
        ({"type": "point", "coordinates": [1, 1]}, "", "", InvalidParameter),
        ({"type": "point", "coordinates": [1, 1]}, "x" * 65, "", InvalidParameter),
        ({"type": "point", "coordinates": [1, 1]}, "x", "n" * 1001, InvalidParameter),
        ({"type": "point", "coordinates": [1, 1]}, "x", 5, InvalidParameter),
    ],
)
def test_annotate_errors(
    make_session: SessionFactory, geometry: Any, label: Any, note: Any, error: type[Exception]
) -> None:
    s = make_session()
    with pytest.raises(error):
        s.annotate(geometry, label, note)


def test_auto_view_off(make_session: SessionFactory, viewer: Profile) -> None:
    s = make_session(profile=viewer.model_copy(update={"auto_view": False}))
    r = s.set_magnification(10)
    assert r.observation is None
    assert r.cost_s == 0.5
    assert r.state["current_view_id"] is None
    with pytest.raises(NoCurrentView):
        s.measure([0, 0], [1, 1])
    with pytest.raises(NoCurrentView):
        s.annotate({"type": "point", "coordinates": [1, 1]}, "x")
    assert s.pan(0.5, 0).cost_s == pytest.approx(0.275)
    v = s.view()
    assert v.cost_s == 0.3
    assert s.measure([0, 0], [10, 0]).observation is not None


# --- budget, report, closing ------------------------------------------------------------------


def test_view_budget(make_session: SessionFactory, viewer: Profile) -> None:
    s = make_session(profile=viewer.model_copy(update={"budget": Budget(max_views=2)}))
    s.set_magnification(10)  # second view
    with pytest.raises(BudgetExhausted):
        s.pan(0.1, 0)
    with pytest.raises(BudgetExhausted):
        s.measure([0, 0], [1, 1])
    assert s.get_state().state["budget_remaining"]["views"] == 0
    assert s.export_trajectory().text
    assert s.submit_report(EXAMPLE).observation is not None


def test_time_budget(make_session: SessionFactory, viewer: Profile) -> None:
    s = make_session(profile=viewer.model_copy(update={"budget": Budget(max_virtual_s=1.0)}))
    s.set_magnification(10)  # 0.3 + 0.8 = 1.1 s: the action that crosses the limit completes
    with pytest.raises(BudgetExhausted, match="time budget"):
        s.view()


def test_submit_report_closes_the_session(make_session: SessionFactory) -> None:
    s = make_session()
    s.set_magnification(10)
    with pytest.raises(ReportInvalid):
        s.submit_report({"task": "sln_metastasis"})
    assert not s.closed
    r = s.submit_report(EXAMPLE)
    assert r.cost_s == 0.0
    receipt = r.observation
    assert receipt is not None
    d = receipt.to_dict()
    assert d["accepted"] is True
    assert d["views_used"] == ["v_0001", "v_0002"]
    assert d["rule_checks"] == []
    assert d["coverage"] is None
    assert s.closed
    for call in (lambda: s.pan(0.1, 0), lambda: s.view(), lambda: s.submit_report(EXAMPLE)):
        with pytest.raises(SessionClosed):
            call()
    assert s.get_state().state["closed"] is True
    assert s.export_trajectory().text


# --- logging --------------------------------------------------------------------------------


def test_events_follow_spec_9(make_session: SessionFactory) -> None:
    ticks = iter(range(1000, 2000, 10))
    s = make_session(wall_ms=lambda: next(ticks))
    s.set_magnification(10)
    s.pan(0.5, 0, client={"think_ms": 1234})
    ev = events(s)[-1]
    assert set(ev) == {
        "type", "seq", "action", "params", "t_virtual_s", "t_wall_ms", "cost_s",
        "state_after", "view_id", "observation", "rule_violations", "error", "client",
    }  # fmt: skip
    assert ev["seq"] == 3
    assert ev["params"] == {"dx": 0.5, "dy": 0}
    assert ev["t_virtual_s"] == pytest.approx(1.675)
    assert ev["t_wall_ms"] == 1020
    assert ev["cost_s"] == 0.575
    assert ev["state_after"] == {
        "center_um": [1536.0, 768.0],
        "mag": 10.0,
        "bbox_um": [1024.0, 384.0, 2048.0, 1152.0],
    }
    assert ev["view_id"] == "v_0003"
    assert ev["client"] == {"think_ms": 1234}
    assert ev["rule_violations"] == []
    assert ev["error"] is None


@pytest.mark.parametrize("client", [{"x": object()}, {"x": "a" * 5000}, {"x": float("nan")}])
def test_bad_client_metadata(make_session: SessionFactory, client: dict[str, Any]) -> None:
    s = make_session()
    with pytest.raises(InvalidParameter):
        s.get_state(client=client)
    assert events(s)[-1]["client"] is None


def test_unserializable_params_are_logged_safely(make_session: SessionFactory) -> None:
    s = make_session()
    with pytest.raises(InvalidParameter):
        s.set_magnification(object())
    assert "unserializable" in events(s)[-1]["params"]


def test_log_file_matches_export(make_session: SessionFactory, tmp_path: Path) -> None:
    path = tmp_path / "logs" / "s.jsonl"
    s = make_session(log_path=path)
    s.set_magnification(5)
    text = s.export_trajectory().text
    s.close()
    assert path.read_text() == text
    lines = [json.loads(x) for x in (text or "").splitlines()]
    assert lines[-1]["action"] == "export_trajectory"
    with pytest.raises(FileExistsError):
        make_session(log_path=path)  # never append to another session's log


def test_state_is_json_serialisable(make_session: SessionFactory) -> None:
    s = make_session()
    json.dumps(s.state_dict())
    json.dumps(s.initial.to_dict())
    assert s.initial.to_dict()["observation"]["type"] == "view"


def test_views_mapping_is_a_snapshot(make_session: SessionFactory) -> None:
    s = make_session()
    snapshot = s.views
    s.view()
    assert list(snapshot) == ["v_0001"]
    assert list(s.views) == ["v_0001", "v_0002"]
