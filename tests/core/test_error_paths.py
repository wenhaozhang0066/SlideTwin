"""Error paths of the core: every failure is a coded SlideTwinError and leaves no half state."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from slidetwin.core import registry as registry_mod
from slidetwin.core import session as session_mod
from slidetwin.core.config import ConfigStore, load_protocol_data
from slidetwin.core.errors import (
    InternalError,
    InvalidParameter,
    ReplayError,
    SlideMetadataError,
    SlideNotFound,
    SlideReadError,
)
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideEntry, SlideRegistry, parse_manifest
from slidetwin.core.replay import replay
from slidetwin.core.slide_source import OpenSlideSource, TiffSlideSource, open_slide
from slidetwin.core.trajectory import TrajectoryWriter, load_trajectory, parse_trajectory
from tests.conftest import CONFIG_DIR, SessionFactory
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff


@pytest.fixture(scope="module")
def tiff(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("err")
    return write_pyramid_tiff(d / "s.tif", pyramid(line_image(512, 512, (10,), (10,)), 2), 0.5)


def boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("backend exploded")


# --- configs -----------------------------------------------------------------------------------


def test_fully_confirmed_protocol_loads() -> None:
    data = yaml.safe_load((CONFIG_DIR / "protocols" / "sln_metastasis.yaml").read_text())
    review = {"status": "confirmed", "reviewed_by": "Dr. A", "reviewed_on": "2026-11-02"}
    for block in ("magnification", "measurement", "coverage", "reading_path_hints", "reporting"):
        data[block].update(review)
    for rules in ("general_rules", "task_rules", "categories"):
        for rule in data[rules]:
            rule.update(review)
    data.update(review)
    assert load_protocol_data(data).status == "confirmed"


# --- registry ----------------------------------------------------------------------------------


def test_list_values_in_eval_only_are_checked_too() -> None:
    manifest = {"slides": [{"id": "s_tumor", "path": "a.tif", "eval_only": {"tags": ["tumor"]}}]}
    with pytest.raises(Exception, match="exposes ground truth"):
        parse_manifest(manifest)


def test_concurrent_first_opens_share_one_handle(
    tiff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = registry_mod._open_auto
    calls: list[int] = []

    def slow_open(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(registry_mod, "_open_auto", slow_open)
    reg = SlideRegistry([SlideEntry(id="s", path=tiff.name)], tiff.parent)
    results: list[Any] = []
    threads = [threading.Thread(target=lambda: results.append(reg.get("s"))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert all(r is results[0] for r in results)


def test_unreadable_file_while_hashing(tiff: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_read(path: Path) -> str:
        raise OSError("I/O error")

    monkeypatch.setattr(registry_mod, "file_sha256", no_read)
    reg = SlideRegistry([SlideEntry(id="s", path=tiff.name, sha256="0" * 64)], tiff.parent)
    with pytest.raises(SlideNotFound, match="could not be read"):
        reg.get("s")


# --- slide sources -----------------------------------------------------------------------------


def test_tiffslide_read_failure_is_a_slide_read_error(tiff: Path) -> None:
    src = open_slide(tiff, name="s")
    assert isinstance(src, TiffSlideSource)

    class Broken:
        def __getitem__(self, key: Any) -> Any:
            raise ValueError("corrupt tile")

    src._arrays[0] = Broken()
    with pytest.raises(SlideReadError, match="'s' could not be decoded at level 0"):
        src.read_level(0, 0, 0, 4, 4)


def test_openslide_failures_are_coded(tiff: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = open_slide(tiff, name="s", backend="openslide")
    assert isinstance(src, OpenSlideSource)
    monkeypatch.setattr(src._slide, "read_region", boom)
    with pytest.raises(SlideReadError):
        src.read_level(0, 0, 0, 4, 4)
    import openslide

    class BadProps:
        properties = property(boom)
        closed = False

        def close(self) -> None:
            BadProps.closed = True

    monkeypatch.setattr(openslide, "OpenSlide", lambda path: BadProps())
    with pytest.raises(SlideMetadataError, match="openslide backend"):
        open_slide(tiff, name="s", backend="openslide")
    assert BadProps.closed


def test_tiffslide_metadata_failure_closes_the_file(
    tiff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tiffslide

    class BadSlide:
        properties = property(boom)
        closed = False

        def close(self) -> None:
            BadSlide.closed = True

    monkeypatch.setattr(tiffslide, "TiffSlide", lambda path: BadSlide())
    with pytest.raises(SlideMetadataError, match="tiffslide backend"):
        open_slide(tiff, name="s")
    assert BadSlide.closed


# --- sessions ------------------------------------------------------------------------------------


def test_values_whose_repr_fails_are_still_reported(make_session: SessionFactory) -> None:
    class NoRepr:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    with pytest.raises(InvalidParameter, match="<NoRepr>"):
        make_session().pan(NoRepr(), 0)


def test_overflowing_crop_rectangle(make_session: SessionFactory, unconstrained: Any) -> None:
    s = make_session(profile=unconstrained)
    with pytest.raises(InvalidParameter, match="out of range"):
        s.crop(1e308, 0, 1e308, 10)


def test_terminated_flag_and_discard(make_session: SessionFactory) -> None:
    s = make_session()
    s.discard_images()  # no effect on a live session
    assert s.get_image("v_0001")
    assert not s.terminated
    s.close()
    assert s.terminated
    s.discard_images()


# --- manager -----------------------------------------------------------------------------------


@pytest.mark.parametrize("with_logs", [True, False])
def test_failed_creation_leaves_nothing_behind(
    synthetic: Any,
    configs: ConfigStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_logs: bool,
) -> None:
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "a" * 64)
    m = SessionManager(registry=reg, configs=configs, log_dir=tmp_path if with_logs else None)
    monkeypatch.setattr(session_mod, "render_region", boom)
    with pytest.raises(InternalError):
        m.create(slide_id="syn", actor="agent:a", viewport_px=(256, 192))
    assert len(m) == 0
    if with_logs:
        assert not any((tmp_path / "sessions").iterdir())
    monkeypatch.undo()
    assert m.create(slide_id="syn", actor="agent:a", viewport_px=(256, 192))  # slot was freed


# --- trajectories --------------------------------------------------------------------------------


def test_writer_refuses_after_close_and_cuts_partial_lines(tmp_path: Path) -> None:
    path = tmp_path / "w.jsonl"
    w = TrajectoryWriter(path)
    w.write({"a": 1})

    class Flaky:
        def __init__(self, fh: Any) -> None:
            self.fh = fh

        def tell(self) -> int:
            return int(self.fh.tell())

        def write(self, text: str) -> int:
            self.fh.write(text[:5])  # a partial line reaches the file, then the disk is full
            raise OSError(28, "No space left on device")

        def truncate(self, pos: int) -> int:
            return int(self.fh.truncate(pos))

        def seek(self, pos: int) -> int:
            return int(self.fh.seek(pos))

        def flush(self) -> None:
            self.fh.flush()

        def close(self) -> None:
            self.fh.close()

    w._fh = Flaky(w._fh)  # type: ignore[assignment]
    with pytest.raises(OSError, match="No space"):
        w.write({"b": 2})
    assert w.text() == '{"a":1}\n'
    w.close()
    assert path.read_text() == '{"a":1}\n'  # the partial line was cut back
    with pytest.raises(RuntimeError):
        w.write({"c": 3})


@pytest.mark.parametrize(
    "lines",
    [
        [{"type": "header", "schema_version": "1.0"}],  # header fields missing
        ["header-ish"],
        [None],
    ],
)
def test_header_checks(lines: list[Any]) -> None:
    with pytest.raises(ReplayError):
        parse_trajectory("".join(json.dumps(x) + "\n" for x in lines))


def test_event_checks(make_session: SessionFactory) -> None:
    text = make_session().export_trajectory().text or ""
    header = text.splitlines()[0]
    for bad in ('"not an event"', '{"type": "note"}', '{"type": "event", "seq": 1}'):
        with pytest.raises(ReplayError):
            parse_trajectory(header + "\n" + bad + "\n")


def test_unreadable_trajectory_file(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="Cannot read"):
        load_trajectory(tmp_path / "missing.jsonl")
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(ReplayError, match="Cannot read"):
        load_trajectory(bad)


def test_replay_reports_invalid_configs_and_actors(
    make_session: SessionFactory, synthetic: Any
) -> None:
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "0" * 64)
    lines = [
        json.loads(x)
        for x in (make_session(slide_id="syn").export_trajectory().text or "").splitlines()
    ]

    bad_config = json.loads(json.dumps(lines))
    bad_config[0]["profile"]["content"]["mag_tolerance"] = 5
    with pytest.raises(ReplayError, match="invalid config"):
        replay("".join(json.dumps(x) + "\n" for x in bad_config), reg)

    bad_actor = json.loads(json.dumps(lines))
    bad_actor[0]["actor"] = "Dr. Smith"
    with pytest.raises(ReplayError, match="cannot be recreated"):
        replay("".join(json.dumps(x) + "\n" for x in bad_actor), reg)


def test_odd_slide_dimensions_render(make_session: SessionFactory) -> None:
    from slidetwin.core.slide_source import ArraySlideSource

    odd = ArraySlideSource([np.full((333, 777, 3), 120, np.uint8)], (1.0, 1.0))
    s = make_session(source=odd, viewport_px=(256, 192))
    assert s.get_thumbnail().observation is not None


def test_event_order_and_first_action(make_session: SessionFactory) -> None:
    text = make_session().export_trajectory().text or ""
    header = text.splitlines()[0]
    out_of_order = {"type": "event", "seq": 2, "action": "create_session", "params": {}}
    with pytest.raises(ReplayError, match="out of order"):
        parse_trajectory(header + "\n" + json.dumps(out_of_order) + "\n")
    not_first = {"type": "event", "seq": 1, "action": "pan", "params": {}}
    with pytest.raises(ReplayError, match="must be create_session"):
        parse_trajectory(header + "\n" + json.dumps(not_first) + "\n")


def test_failed_truncate_is_tolerated(tmp_path: Path) -> None:
    w = TrajectoryWriter(tmp_path / "t.jsonl")

    class NoTruncate:
        def __init__(self, fh: Any) -> None:
            self.fh = fh

        def tell(self) -> int:
            return int(self.fh.tell())

        def write(self, text: str) -> int:
            raise OSError(28, "No space left on device")

        def truncate(self, pos: int) -> int:
            raise OSError(5, "I/O error")

        def flush(self) -> None:
            self.fh.flush()

        def close(self) -> None:
            self.fh.close()

    w._fh = NoTruncate(w._fh)  # type: ignore[assignment]
    with pytest.raises(OSError, match="No space"):
        w.write({"a": 1})
    assert w.records == []
    w.close()


def test_non_text_eval_only_values_are_ignored_by_the_leak_check() -> None:
    manifest = {
        "slides": [{"id": "s5", "path": "a.tif", "eval_only": {"n": 5, "tags": ["normal"]}}]
    }
    assert parse_manifest(manifest)[0].id == "s5"
