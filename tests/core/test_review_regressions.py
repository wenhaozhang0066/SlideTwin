"""Regression tests for the confirmed findings of the M1 adversarial review (ids in brackets)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core import session as session_mod
from slidetwin.core.config import Profile, Protocol
from slidetwin.core.errors import (
    ImageNotFound,
    InternalError,
    InvalidParameter,
    ReplayError,
    ReportInvalid,
    SessionClosed,
    SlideMetadataError,
    SlideReadError,
)
from slidetwin.core.geometry import BBox, mpp_for_mag
from slidetwin.core.images import MemoryImageStore
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.render import render_region
from slidetwin.core.replay import replay
from slidetwin.core.report import Report
from slidetwin.core.session import Session
from slidetwin.core.slide_source import ArraySlideSource, open_slide
from slidetwin.core.trajectory import parse_trajectory
from tests.conftest import SessionFactory
from tests.core.test_report import EXAMPLE
from tests.helpers.synth import line_source

# --- rendering --------------------------------------------------------------------------------


def test_partial_edge_block_does_not_move_the_slide_edge() -> None:
    """[GR-1] Padding a slide past its edge with fill-coloured pixels changes no output pixel."""
    rng = np.random.default_rng(1)
    odd = rng.integers(0, 255, (1001, 1001, 3), dtype=np.uint8)
    padded = np.full((1024, 1024, 3), 255, dtype=np.uint8)
    padded[:1001, :1001] = odd
    a = ArraySlideSource([odd], (0.25, 0.25))
    b = ArraySlideSource([padded], (0.25, 0.25))
    for mag in (10.0, 5.0, 1.25):  # pre-reduction factors k = 4, 8, 32
        mpp = mpp_for_mag(mag)
        bbox = BBox(250.25 - 40 * mpp, 0.0, 250.25 + 24 * mpp, 32 * mpp)  # spans the right edge
        np.testing.assert_array_equal(
            render_region(a, bbox, (64, 32)), render_region(b, bbox, (64, 32))
        )


def test_anisotropic_slide_does_not_allocate_a_huge_canvas() -> None:
    """[RS-13] Per-axis pre-reduction keeps the canvas near the output size."""
    src = ArraySlideSource([np.full((4000, 400, 3), 200, np.uint8)], (0.25, 2.5))
    img = render_region(src, BBox(0.0, 0.0, 100.0, 10000.0), (40, 40))
    assert img.shape == (40, 40, 3)


def test_extreme_aspect_thumbnail_completes(make_session: SessionFactory) -> None:
    """[RS-13] A 1 x 100000 px single-level slide still renders its thumbnail."""
    thin = ArraySlideSource([np.full((1, 100_000, 3), 90, np.uint8)], (0.5, 0.5))
    s = make_session(source=thin, viewport_px=(256, 192))
    obs = s.get_thumbnail().observation
    assert obs is not None


def test_thumbnail_of_small_slide_is_not_upsampled(make_session: SessionFactory) -> None:
    """[GR-2, SC-04] A 300 x 200 px slide gets a 300 x 200 thumbnail, not 1024 px."""
    small = line_source(width=300, height=200, mpp=1.0, n_levels=1, xs=(100,), ys=())
    s = make_session(source=small, viewport_px=(256, 192))
    obs = s.get_thumbnail().observation
    assert obs is not None
    assert obs.to_dict()["size_px"] == [300, 200]
    assert min(obs.to_dict()["um_per_px"]) >= 1.0


# --- slide files ------------------------------------------------------------------------------


def test_planar_rgb_tiff_is_a_clean_error(tmp_path: Path) -> None:
    """[GR-3, RS-08] tiffslide cannot read planar RGB: a SlideMetadataError naming only the id."""
    path = tmp_path / "tumor_planar.tif"
    data = np.zeros((3, 256, 256), np.uint8)
    tifffile.imwrite(
        path, data, photometric="rgb", planarconfig="separate", tile=(256, 256),
        resolution=(40000, 40000), resolutionunit="CENTIMETER",
    )  # fmt: skip
    with pytest.raises(SlideMetadataError) as err:
        open_slide(path, name="s_planar", backend="tiffslide")
    assert "s_planar" in err.value.message
    assert "tumor" not in err.value.message
    assert err.value.hint is not None
    assert "openslide" in err.value.hint
    assert open_slide(path, name="s_planar", backend="openslide").dimensions == (256, 256)


def test_decode_errors_are_slide_read_errors(
    make_session: SessionFactory, synthetic: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[RS-04, SC-05] A failing tile read is logged, coded and changes nothing."""
    s = make_session()
    before = s.state_dict()

    def bad_read(*args: Any, **kwargs: Any) -> Any:
        raise SlideReadError("Slide 'x' could not be decoded at level 0 (DeflateError).")

    monkeypatch.setattr(synthetic, "read_level", bad_read)
    with pytest.raises(SlideReadError):
        s.set_magnification(10)
    assert s.state_dict() == before
    assert s.history[-1]["error"]["code"] == "SLIDE_READ_ERROR"


# --- logging and replay integrity ---------------------------------------------------------------


def test_successful_actions_log_canonical_params_and_replay(
    make_session: SessionFactory, viewer: Profile, protocol: Protocol, synthetic: Any
) -> None:
    """[SC-02, DR-06] Non-JSON inputs that an action accepts are logged in canonical form."""
    s = make_session(slide_id="syn")
    s.set_magnification(np.float64(10.0))
    s.annotate({"type": "polygon", "coordinates": ((1, 1), (5, 1), (5, 5))}, "x")
    s.measure((0, 0), [3, 4])
    s.submit_report(Report.model_validate(EXAMPLE))
    events = [r for r in s.history if r["type"] == "event"]
    assert events[2]["params"]["geometry"] == {
        "type": "polygon",
        "coordinates": [[1.0, 1.0], [5.0, 1.0], [5.0, 5.0]],
    }
    assert events[3]["params"] == {"p1_px": [0.0, 0.0], "p2_px": [3.0, 4.0]}
    assert events[4]["params"]["report"]["conclusion"]["category"] == "micro"
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "0" * 64)
    assert replay(s.export_trajectory().text or "", reg).ok


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "point", "coordinates": [5, 5], "style": float("nan")},
        {"type": "point", "coordinates": [5, 5], "style": {"color": "red"}},
        {"type": "point"},
    ],
)
def test_geometry_must_have_exactly_type_and_coordinates(
    make_session: SessionFactory, geometry: dict[str, Any]
) -> None:
    """[RS-06] Unknown geometry keys are refused, and the refusal is logged as valid JSON."""
    s = make_session()
    with pytest.raises(InvalidParameter, match="exactly the keys"):
        s.annotate(geometry, "x")
    json.dumps(s.history[-1], allow_nan=False)


def test_huge_integers_are_parameter_errors(make_session: SessionFactory) -> None:
    """[SC-05, DR-08, RS-04] 10**400 is refused as INVALID_PARAMETER and logged."""
    s = make_session()
    for call in (
        lambda: s.pan(10**400, 0),
        lambda: s.move_to(0, -(10**400)),
        lambda: s.set_magnification(10**400),
        lambda: s.measure([10**400, 0], [0, 0]),
    ):
        with pytest.raises(InvalidParameter):
            call()
    errors = [r["error"]["code"] for r in s.history if r["type"] == "event" and r["error"]]
    assert errors == ["INVALID_PARAMETER"] * 4


def test_construction_and_lookup_type_errors_are_coded(make_session: SessionFactory) -> None:
    """[RS-04] Wrong types fail as SlideTwinErrors, not TypeError."""
    with pytest.raises(InvalidParameter):
        make_session(viewport_px=1024)
    s = make_session()
    with pytest.raises(ImageNotFound):
        s.get_image(["v_0001"])  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["a\u2028b", "a\u2029b", "a\x85b", "\ud800 lone surrogate"])
def test_any_text_keeps_the_log_parseable(
    make_session: SessionFactory, synthetic: Any, text: str, tmp_path: Path
) -> None:
    """[DR-01, DR-05, RS-02] Line separators and surrogates cannot split or break the JSONL."""
    log = tmp_path / "t.jsonl"
    s = make_session(slide_id="syn", log_path=log)
    s.annotate({"type": "point", "coordinates": [1, 1]}, "x", text, client={"note": text})
    with pytest.raises(InvalidParameter):
        s.set_magnification(text)
    report = json.loads(json.dumps(EXAMPLE))
    report["free_text"] = text
    if "\ud800" in text:
        with pytest.raises(ReportInvalid):  # not valid Unicode: refused, and the refusal logged
            s.submit_report(report)
    else:
        s.submit_report(report)
    s.close()
    raw = log.read_bytes()
    assert raw.isascii()
    _, events = parse_trajectory(raw.decode("ascii"))
    assert events[1]["params"]["note"] == text
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "0" * 64)
    assert replay(log, reg).ok


def test_invalid_client_metadata_does_not_change_the_replay(
    make_session: SessionFactory, synthetic: Any
) -> None:
    """[DR-04] Replay ignores client metadata, so it must never decide an action."""
    s = make_session(slide_id="syn")
    s.pan(0.1, 0, client={"reasoning": "x" * 10_000})
    s.pan(0.1, 0, client={"t": float("nan")})
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "0" * 64)
    assert replay(s.export_trajectory().text or "", reg).ok


def test_commit_failures_roll_back_and_are_logged(
    make_session: SessionFactory, synthetic: Any
) -> None:
    """[DR-02, RS-03] A failing image store leaves no half-applied action and no seq gap."""

    class FlakyStore(MemoryImageStore):
        fail = False

        def put(self, image_id: str, data: bytes) -> None:
            if self.fail:
                raise OSError(28, "No space left on device")
            super().put(image_id, data)

    store = FlakyStore()
    s = make_session(slide_id="syn", images=store)
    before = s.state_dict()
    store.fail = True
    with pytest.raises(InternalError) as err:
        s.set_magnification(10)
    assert err.value.context is not None
    assert err.value.context["seq"] == 2
    assert s.state_dict() == before
    store.fail = False
    r = s.set_magnification(10)
    assert r.seq == 3
    assert r.observation is not None
    assert r.observation.to_dict()["view_id"] == "v_0002"  # no id was burnt
    text = s.export_trajectory().text or ""
    _, events = parse_trajectory(text)
    assert events[1]["error"]["code"] == "INTERNAL"
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "0" * 64)
    report = replay(text, reg)
    assert report.ok
    assert report.skipped == 1


def test_a_session_that_cannot_log_stops(
    make_session: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[DR-02] If even the failure cannot be written, the session refuses further actions."""
    s = make_session()

    def no_disk(record: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(s._writer, "write", no_disk)
    with pytest.raises(InternalError):
        s.set_magnification(10)
    monkeypatch.undo()
    with pytest.raises(InternalError, match="stopped"):
        s.view()


def test_close_is_final_and_waits_for_the_running_action(
    make_session: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[SC-08, DR-03, RS-11, R3] close() ends the session after the action in progress."""
    log = tmp_path / "c.jsonl"
    s = make_session(log_path=log)
    real = session_mod.render_region
    started = threading.Event()

    def slow(*args: Any, **kwargs: Any) -> Any:
        started.set()
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_mod, "render_region", slow)
    worker = threading.Thread(target=lambda: s.set_magnification(10))
    worker.start()
    started.wait(5)
    s.close()  # must wait for the action, which is then fully on disk
    worker.join(5)
    lines = log.read_text().splitlines()
    assert json.loads(lines[-1])["action"] == "set_magnification"
    with pytest.raises(SessionClosed):
        s.pan(0.1, 0)
    with pytest.raises(SessionClosed):
        s.get_image("v_0001")
    assert len(log.read_text().splitlines()) == len(lines)  # nothing after close


@pytest.mark.parametrize(
    "edit",
    [
        lambda h, e: h.pop("slide"),
        lambda h, e: h.update(actor=5),
        lambda h, e: h["profile"].update(content="x"),
        lambda h, e: h.update(viewport_px=[1, 2, 3]),
        lambda h, e: e[1].update(params="pan"),
        lambda h, e: e[1].update(seq=True),
        lambda h, e: e[1].pop("action"),
        lambda h, e: e[1].update(error="bad"),
    ],
)
def test_malformed_logs_are_replay_errors(
    make_session: SessionFactory, synthetic: Any, edit: Any
) -> None:
    """[DR-12, RS-12] Structural damage raises ReplayError, never KeyError or TypeError."""
    s = make_session(slide_id="syn")
    s.pan(0.2, 0)
    lines = [json.loads(x) for x in (s.export_trajectory().text or "").splitlines()]
    edit(lines[0], lines[1:])
    text = "".join(json.dumps(x) + "\n" for x in lines)
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "0" * 64)
    with pytest.raises(ReplayError):
        replay(text, reg)


def test_deeply_nested_log_is_a_replay_error() -> None:
    """[RS-12] Recursion limits while parsing become ReplayError."""
    with pytest.raises(ReplayError):
        parse_trajectory("[" * 100_000 + "]" * 100_000 + "\n")


def test_rejected_inputs_are_bounded_in_messages(make_session: SessionFactory) -> None:
    """[RS-05] Echoed values are shortened in error messages."""
    s = make_session()
    with pytest.raises(InvalidParameter) as err:
        s.pan("x" * 1_000_000, 0)
    assert len(err.value.message) < 200


# --- input formats ------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["agent:bob\n", "human:a\u0660"])
def test_actor_pattern_is_exact(make_session: SessionFactory, actor: str) -> None:
    """[RS-15] No trailing newline, no non-ASCII digits."""
    with pytest.raises(InvalidParameter):
        make_session(actor=actor)


@pytest.mark.parametrize("image_id", ["v_0001\n", "v_\u0660\u0661\u0662\u0663", "v_1"])
def test_image_ids_are_exact(make_session: SessionFactory, image_id: str) -> None:
    """[RS-15]"""
    with pytest.raises(ImageNotFound):
        make_session().get_image(image_id)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("conclusion", "largest_deposit_mm"), True),
        (("conclusion", "largest_deposit_mm"), "1.3"),
        (("claims", 0, "evidence"), {"v_0042"}),
        (("claims", 0, "evidence"), ["v_0042\nx"]),
        (("task",), b"sln_metastasis"),
    ],
)
def test_report_validation_is_strict(path: tuple[Any, ...], value: Any) -> None:
    """[RS-14] Booleans, numeric strings, sets and bytes are refused, as in the JSON Schema."""
    from slidetwin.core.report import validate_report

    data = json.loads(json.dumps(EXAMPLE))
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ReportInvalid):
        validate_report(data, expected_task="sln_metastasis")


def test_view_budget_does_not_block_measuring_the_last_view(
    make_session: SessionFactory, viewer: Profile
) -> None:
    """[SC-07] Checked in detail in test_session; here only that the REST-visible order holds."""
    from slidetwin.core.config import Budget

    s = make_session(profile=viewer.model_copy(update={"budget": Budget(max_views=1)}))
    assert s.measure([0, 0], [10, 0]).observation is not None


def test_session_without_slide_dimensions_stays_internal(
    make_session: SessionFactory, protocol: Protocol, viewer: Profile
) -> None:
    """Unexpected exceptions inside planning become INTERNAL and change nothing."""
    s = make_session()

    class Boom(RuntimeError):
        pass

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise Boom("internal detail /secret")

    s_render = session_mod.render_region
    try:
        session_mod.render_region = explode
        with pytest.raises(InternalError) as err:
            s.view()
    finally:
        session_mod.render_region = s_render
    assert "secret" not in err.value.message
    assert s.history[-1]["error"]["code"] == "INTERNAL"


def _unused(_: Session) -> None:  # keeps the Session import used for type checkers
    pass
