"""Replay: a session reproduced from its log alone gives identical states, costs and pixels."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from slidetwin.core.config import Profile, Protocol
from slidetwin.core.errors import ReplayError, SlideTwinError
from slidetwin.core.images import MemoryImageStore
from slidetwin.core.registry import SlideEntry, SlideRegistry
from slidetwin.core.replay import replay
from slidetwin.core.session import Session
from slidetwin.core.trajectory import TrajectoryWriter, parse_trajectory
from tests.core.test_report import EXAMPLE
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tiff_registry(tmp_path_factory: pytest.TempPathFactory) -> SlideRegistry:
    d = tmp_path_factory.mktemp("replay")
    levels = pyramid(line_image(2048, 1536, (300, 1000), (200, 777)), 5)
    write_pyramid_tiff(d / "syn40.tif", levels, 0.25)
    entries = [SlideEntry(id="syn40", path="syn40.tif")]
    return SlideRegistry(entries, d, cache_dir=d / "cache")


def new_session(reg: SlideRegistry, profile: Profile, protocol: Protocol, **kw: Any) -> Session:
    slide = reg.get("syn40")
    return Session(
        session_id="orig",
        slide_id="syn40",
        source=slide.source,
        slide_sha256=slide.sha256,
        profile=profile,
        protocol=protocol,
        actor="human:reader01",
        images=MemoryImageStore(),
        viewport_px=(512, 384),
        **kw,
    )


def scripted(s: Session) -> str:
    """A session that touches every action, including failures."""
    s.get_thumbnail()
    s.set_magnification(10)
    s.pan(0.5, 0.25, client={"think_ms": 50})
    s.move_to(200.3, 150.7)
    s.set_magnification(40)
    for bad in (lambda: s.set_magnification(7), lambda: s.measure([0, 0], [9999, 0])):
        with pytest.raises(SlideTwinError):
            bad()
    s.view()
    s.measure([10.5, 20.25], [300, 200])
    s.annotate({"type": "polygon", "coordinates": [[1, 1], [50, 1], [50, 40]]}, "cluster", "x")
    s.get_state()
    s.submit_report(EXAMPLE)
    with pytest.raises(SlideTwinError):
        s.pan(0.1, 0)
    return s.export_trajectory().text or ""


def test_replay_reproduces_every_event(
    tiff_registry: SlideRegistry, viewer: Profile, protocol: Protocol
) -> None:
    text = scripted(new_session(tiff_registry, viewer, protocol))
    report = replay(text, tiff_registry)
    assert report.ok, report.mismatches
    assert report.events == 15
    assert report.images_checked == 7  # 6 views + 1 thumbnail


def test_replay_from_file_and_unconstrained_crops(
    tiff_registry: SlideRegistry, unconstrained: Profile, protocol: Protocol, tmp_path: Path
) -> None:
    log = tmp_path / "u.jsonl"
    s = new_session(tiff_registry, unconstrained, protocol, log_path=log)
    s.get_thumbnail()
    s.crop(10.1, 20.2, 300, 150)
    s.crop(100, 100, 20, 20)
    s.measure([0, 0], [10, 10])
    s.close()
    report = replay(log, tiff_registry)
    assert report.ok, report.mismatches


def test_replay_with_auto_view_off(
    tiff_registry: SlideRegistry, viewer: Profile, protocol: Protocol
) -> None:
    s = new_session(tiff_registry, viewer.model_copy(update={"auto_view": False}), protocol)
    s.set_magnification(20)
    with pytest.raises(SlideTwinError):
        s.measure([0, 0], [1, 1])
    s.view()
    s.measure([0, 0], [1, 1])
    assert replay(s.export_trajectory().text or "", tiff_registry).ok


def tampered(text: str, fn: Any) -> str:
    lines = [json.loads(x) for x in text.splitlines()]
    fn(lines)
    return "".join(json.dumps(x) + "\n" for x in lines)


@pytest.fixture
def good_log(tiff_registry: SlideRegistry, viewer: Profile, protocol: Protocol) -> str:
    s = new_session(tiff_registry, viewer, protocol)
    s.set_magnification(10)
    s.pan(0.5, 0)
    s.measure([0, 0], [100, 0])
    return s.export_trajectory().text or ""


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        ("observation", lambda ls: ls[2]["observation"].update(pixel_sha256="0" * 64)),
        ("cost_s", lambda ls: ls[3].update(cost_s=9.9)),
        ("state_after", lambda ls: ls[3]["state_after"].update(mag=20.0)),
        ("observation", lambda ls: ls[4]["observation"].update(length_um=1.0)),
        ("error", lambda ls: ls[3].update(error={"code": "X", "message": "y"})),
    ],
)
def test_replay_detects_tampering(
    good_log: str, tiff_registry: SlideRegistry, field: str, edit: Any
) -> None:
    report = replay(tampered(good_log, edit), tiff_registry)
    assert not report.ok
    assert field in {m["field"] for m in report.mismatches}


def test_logs_without_provenance_and_new_versions_still_replay(
    good_log: str, tiff_registry: SlideRegistry
) -> None:
    def old_log(lines: list[dict[str, Any]]) -> None:
        del lines[0]["slide"]["provenance"]  # written before M8

    def upgraded(lines: list[dict[str, Any]]) -> None:
        lines[0]["slide"]["provenance"]["versions"] = {"tiffslide": "0.0.1"}

    assert replay(tampered(good_log, old_log), tiff_registry).ok
    assert replay(tampered(good_log, upgraded), tiff_registry).ok


def test_the_header_names_the_file_set_hash_of_file_sets_only(
    tiff_registry: SlideRegistry, viewer: Profile, protocol: Protocol
) -> None:
    def header(**kw: Any) -> dict[str, Any]:
        s = new_session(tiff_registry, viewer, protocol, **kw)
        s.get_thumbnail()
        first = (s.export_trajectory().text or "").splitlines()[0]
        slide: dict[str, Any] = json.loads(first)["slide"]
        return slide

    assert "fileset_sha256" not in header()
    assert header(slide_fileset_sha256="a" * 64)["fileset_sha256"] == "a" * 64


def test_jpeg_hash_differences_are_tolerated(good_log: str, tiff_registry: SlideRegistry) -> None:
    edited = tampered(good_log, lambda ls: ls[2]["observation"].update(jpeg_sha256="f" * 64))
    assert replay(edited, tiff_registry).ok


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda ls: ls[0]["slide"].update(sha256="0" * 64), "differs from the file"),
        (lambda ls: ls[0]["slide"].update(fileset_sha256="0" * 64), "file-set hash mismatch"),
        (lambda ls: ls[0]["slide"]["provenance"].update(reader="other"), "read differently"),
        (lambda ls: ls[0]["slide"].update(provenance="tiffslide"), "read differently"),
        (lambda ls: ls[0]["profile"]["content"].update(mag_tolerance=0.01), "profile"),
        (lambda ls: ls[0]["protocol"]["content"].update(version="9"), "protocol"),
        (lambda ls: ls[2].update(action="teleport"), "Unknown action"),
    ],
)
def test_replay_refuses_inconsistent_logs(
    good_log: str, tiff_registry: SlideRegistry, edit: Any, message: str
) -> None:
    with pytest.raises(ReplayError, match=message):
        replay(tampered(good_log, edit), tiff_registry)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json\n",
        '{"type": "event"}\n',
        '{"type": "header", "schema_version": "0.1"}\n',
        '{"type": "header", "schema_version": "1.0"}\n{"type": "event", "seq": 2}\n',
        '{"type": "header", "schema_version": "1.0"}\n',
        '{"type": "header", "schema_version": "1.0"}\n'
        '{"type": "event", "seq": 1, "action": "pan"}\n',
    ],
)
def test_parse_rejects_malformed_logs(text: str) -> None:
    with pytest.raises(ReplayError):
        parse_trajectory(text)


def test_writer_memory_only() -> None:
    w = TrajectoryWriter()
    w.write({"a": 1})
    assert w.text() == '{"a":1}\n'
    w.close()


@pytest.mark.testdata
def test_m1_acceptance_script(cmu_small_path: Path, tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import demo_m1
    finally:
        sys.path.pop(0)
    # The test slide lives wherever fetch_test_data.py put it (CI: data/testdata), not data/slides.
    slide_dir = str(cmu_small_path.parent.parent)
    assert demo_m1.main(["--out", str(tmp_path / "demo"), "--slide-dir", slide_dir]) == 0
