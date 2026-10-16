"""Olympus VSI reader (M8): the file set, scenes by index, the validated variant, 2^k levels, and
reads through the slideio worker. CI runs a fake slideio in the worker; OS-2 (tier 3) checks the
real library against the survey's numbers."""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

import slidetwin.readers.slideio_worker as worker
from slidetwin.core.errors import (
    SlideIntegrityError,
    SlideMetadataError,
    SlideNotFound,
    SlideReadError,
)
from slidetwin.core.fileset import fileset_sha256
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.registry import SlideRegistry, file_sha256
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.vsi import VsiSource, open_reader
from tests.conftest import sample_path
from tests.helpers.fake_slideio import pixels, scene_spec, write_spec

MATCH = FormatMatch("olympus-vsi", "slideio")
STACKS = ("stack1", "stack10001")  # the overview, then the acquisition (OS-2's layout)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "LIBRARY", "tests.helpers.fake_slideio")


def write_vsi(
    folder: Path,
    scenes: list[dict[str, Any]] | None = None,
    *,
    stem: str = "slide",
    stacks: tuple[str, ...] = STACKS,
    **behaviour: Any,
) -> Path:
    """A .vsi (a TIFF header is all the reader looks at) and its _<stem>_ folder, described to the
    fake slideio."""
    folder.mkdir(parents=True, exist_ok=True)
    vsi = folder / f"{stem}.vsi"
    vsi.write_bytes(b"II*\x00" + bytes(60))
    for stack in stacks:
        (folder / f"_{stem}_" / stack).mkdir(parents=True)
        (folder / f"_{stem}_" / stack / "frame_t.ets").write_bytes(b"SIS\x00" + bytes(60))
    write_spec(vsi, [scene_spec()] if scenes is None else scenes, driver="VSI", **behaviour)
    return vsi


def open_vsi(path: Path, **kw: Any) -> VsiSource:
    override = kw.pop("mpp_override", None)
    return open_reader(path, MATCH, name="s1", mpp_override=override, options=ReaderOptions(**kw))


# --- the reader on the fake library ----------------------------------------------------------


@pytest.mark.usefixtures("fake")
def test_levels_pixel_size_provenance_and_file_set(tmp_path: Path) -> None:
    vsi = write_vsi(tmp_path, [scene_spec((1000, 601), levels=4)])
    assert sniff(vsi) == MATCH
    src = open_source(vsi, MATCH, name="s1")
    assert isinstance(src, VsiSource)
    assert src.level_dimensions == ((1000, 601), (500, 300), (250, 150), (125, 75))
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(4))
    assert src.mpp == pytest.approx((0.5, 0.5))
    assert src.provenance == {
        "reader": "slideio",
        "format": "olympus-vsi",
        "scene": 0,
        "mpp_source": "vsi stack calibration",
        "fill": [255, 255, 255],
        "downsamples": [1, 2, 4, 8],
        "compression": "Jpeg2000",
        "versions": {"slideio": "2.10.0"},
    }
    assert src.fileset == (
        tmp_path,
        ("slide.vsi", "_slide_/stack1/frame_t.ets", "_slide_/stack10001/frame_t.ets"),
    )
    for level, (w, h) in enumerate(src.level_dimensions):
        np.testing.assert_array_equal(
            src.read_level(level, 3, 2, w - 3, h - 2), pixels(0, level, 3, 2, w - 3, h - 2)
        )
    src.close()


@pytest.mark.usefixtures("fake")
def test_one_slide_per_scene(tmp_path: Path) -> None:
    vsi = write_vsi(tmp_path, [scene_spec(), scene_spec((600, 400), levels=2)])
    with pytest.raises(SlideMetadataError, match=r"holds 2 scenes \(0-1\)") as err:
        open_vsi(vsi)
    assert "scene" in (err.value.hint or "")
    src = open_vsi(vsi, scene=1)
    assert src.level_dimensions == ((600, 400), (300, 200))
    assert src.provenance["scene"] == 1
    np.testing.assert_array_equal(src.read_level(1, 0, 0, 30, 20), pixels(1, 1, 0, 0, 30, 20))
    src.close()
    with pytest.raises(SlideMetadataError, match="has no scene 2; its scenes are 0-1"):
        open_vsi(vsi, scene=2)
    for choice in ({"plane": 0}, {"channels": (0,)}):
        with pytest.raises(SlideMetadataError, match="to choose"):
            open_vsi(vsi, **choice)


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize(
    ("scene", "message"),
    [
        (scene_spec(channels=1), "stores 1-channel uint8 pixels"),
        (scene_spec(dtype="uint16"), "stores 3-channel uint16 pixels"),
        (scene_spec(z=2), r"z-stack or a time series \(z 2, t 1\)"),
        (scene_spec(t=3), r"\(z 1, t 3\)"),
        ({**scene_spec(), "levels": [[1000, 600], [400, 240]]}, "not 2\\^k reductions"),
        ({**scene_spec(), "levels": [[1000, 600], [500, 300], [500, 300]]}, "not 2\\^k"),
        ({**scene_spec(), "levels": [[999, 600]]}, "not 2\\^k reductions"),
        ({**scene_spec(), "levels": [[1000, 600], [0, 300]]}, "not 2\\^k reductions"),
        ({**scene_spec(), "levels": []}, "not 2\\^k reductions"),
        (scene_spec(resolution=(0.0, 0.0)), "refusing to guess"),
        (scene_spec(resolution=(1e-3, 1e-3)), "refusing to guess"),
    ],
)
def test_refused_variants(tmp_path: Path, scene: dict[str, Any], message: str) -> None:
    with pytest.raises(SlideMetadataError, match=message):
        open_vsi(write_vsi(tmp_path, [scene]))


@pytest.mark.usefixtures("fake")
def test_pixel_size_override_and_an_empty_slide(tmp_path: Path) -> None:
    src = open_vsi(write_vsi(tmp_path, [scene_spec(resolution=(0, 0))]), mpp_override=(0.3, 0.3))
    assert (src.mpp, src.provenance["mpp_source"]) == ((0.3, 0.3), "mpp_override")
    src.close()
    with pytest.raises(SlideNotFound, match="slideio finds no image in it"):
        open_vsi(write_vsi(tmp_path / "empty", []))


def test_the_file_set_must_be_complete(tmp_path: Path) -> None:
    with pytest.raises(SlideNotFound, match="is incomplete: the folder of image files") as err:
        open_vsi(write_vsi(tmp_path / "a", stacks=()))
    assert "_<name>_" in (err.value.hint or "")
    no_frame = write_vsi(tmp_path / "b", stacks=())
    (tmp_path / "b" / "_slide_" / "stack1").mkdir(parents=True)
    (tmp_path / "b" / "_slide_" / "stack1" / "other.ets").write_bytes(b"")
    (tmp_path / "b" / "_slide_" / "frame_t.ets").write_bytes(b"")  # not in a stack folder
    with pytest.raises(SlideNotFound, match="is incomplete"):
        open_vsi(no_frame)
    renamed = write_vsi(tmp_path / "c")
    renamed.rename(tmp_path / "c" / "other.vsi")
    with pytest.raises(SlideNotFound, match="is incomplete"):
        open_vsi(tmp_path / "c" / "other.vsi")
    junk = tmp_path / "junk.vsi"
    junk.write_bytes(b"<?xml version='1.0'?>")
    with pytest.raises(SlideMetadataError, match=r"extension \.vsi but is not an Olympus VSI"):
        open_vsi(junk)


@pytest.mark.parametrize("stack", [os.fsdecode(b"stack\xff"), "stack\n2", "stack\\2"])
def test_odd_file_names_are_refused_never_echoed(tmp_path: Path, stack: str) -> None:
    vsi = write_vsi(tmp_path, stacks=("stack1", stack))
    with pytest.raises(SlideMetadataError, match="whose name SlideTwin will not open") as err:
        open_vsi(vsi)
    assert "stack" not in str(err.value)


@pytest.mark.usefixtures("fake")
def test_read_failures(tmp_path: Path) -> None:
    src = open_vsi(write_vsi(tmp_path, read="error"))
    with pytest.raises(SlideReadError, match=r"Slide 's1' could not be decoded .*Runtime") as e:
        src.read_level(0, 0, 0, 8, 8)
    assert "secret" not in str(e.value)
    src.close()
    src = open_vsi(write_vsi(tmp_path / "gray", read="gray"))
    with pytest.raises(SlideReadError, match="other samples than it announced"):
        src.read_level(0, 0, 0, 8, 8)
    src.close()


@pytest.mark.usefixtures("fake")
def test_a_crash_fails_one_read(tmp_path: Path) -> None:
    marker = tmp_path / "crash"
    src = open_vsi(write_vsi(tmp_path, read="crash-once", marker=str(marker)))
    marker.touch()
    with pytest.raises(SlideReadError, match="the slideio worker stopped"):
        src.read_level(1, 0, 0, 64, 64)
    np.testing.assert_array_equal(src.read_level(1, 0, 0, 64, 64), pixels(0, 1, 0, 0, 64, 64))
    src.close()


def test_a_missing_slideio_names_the_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "slideio" else real(name, *a)
    )
    for module in ("slidetwin.readers.vsi", "slidetwin.readers.slideio_worker"):
        monkeypatch.delitem(sys.modules, module)  # imported again, as in a fresh process
    with pytest.raises(SlideMetadataError, match=r"library \(slideio\) is not installed") as err:
        open_source(write_vsi(tmp_path), MATCH, name="s1")
    assert 'pip install "slidetwin[slideio]"' in (err.value.hint or "")


@pytest.mark.usefixtures("fake")
def test_the_registry_hashes_and_pins_the_file_set(tmp_path: Path) -> None:
    vsi = write_vsi(tmp_path / "vsi")
    src = open_vsi(vsi)
    assert src.fileset is not None
    expected = fileset_sha256(*src.fileset, file_sha256)
    src.close()
    entry: dict[str, Any] = {"id": "vsi1", "path": "vsi/slide.vsi"}
    for pinned, error in ((expected, None), ("0" * 64, SlideIntegrityError)):
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(yaml.safe_dump({"slides": [{**entry, "fileset_sha256": pinned}]}))
        registry = SlideRegistry.from_manifest(manifest, tmp_path)
        if error is None:
            assert registry.get("vsi1").sha256 == expected
        else:
            with pytest.raises(error):
                registry.get("vsi1")
        registry.close()


# --- OS-2 (OpenSlide test data, "distributable", tier 3) -----------------------------------------

OS2 = "Olympus/OS-2/OS-2.vsi"


def box(a: np.ndarray, f: int) -> np.ndarray:
    h, w = a.shape[0] // f, a.shape[1] // f
    return a[: h * f, : w * f].reshape(h, f, w, f, 3).astype(np.float64).mean(axis=(1, 3))


def mean_rgb(a: np.ndarray) -> list[float]:
    return a.reshape(-1, 3).mean(axis=0).tolist()  # type: ignore[no-any-return]


@pytest.mark.testdata
def test_os2_levels_pixel_size_and_pixels() -> None:
    path = sample_path(OS2, tier=3)
    assert sniff(path) == MATCH
    src = open_source(path, MATCH, name="s1")
    assert isinstance(src, VsiSource)
    assert src.level_dimensions == (  # floor sizes (Bio-Formats rounds up)
        (48502, 77578),
        (24251, 38789),
        (12125, 19394),
        (6062, 9697),
        (3031, 4848),
        (1515, 2424),
        (757, 1212),
        (378, 606),
        (189, 303),
    )
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(9))
    assert src.mpp == pytest.approx((0.346056, 0.346053), abs=1e-12)
    assert (src.provenance["scene"], src.provenance["compression"]) == (0, "Jpeg2000")
    assert src.fileset == (
        path.parent,
        ("OS-2.vsi", "_OS-2_/stack1/frame_t.ets", "_OS-2_/stack10001/frame_t.ets"),
    )
    # the survey's numbers: a level-0 region and the whole coarsest level
    region = src.read_level(0, 23995, 38533, 512, 512)
    assert mean_rgb(region) == pytest.approx([203.9, 206.9, 209.4], abs=0.05)
    assert mean_rgb(src.read_level(8, 0, 0, 189, 303)) == pytest.approx(
        [213.4, 214.8, 215.1], abs=0.05
    )
    # levels are 2^k reductions aligned at the origin: no shift fits level 0's box mean better
    size, r = 128, 2
    for level in (1, 2, 3):
        f = 2**level
        x, y = 24000 // f - size // 2, 38000 // f - size // 2
        ref = box(src.read_level(0, x * f, y * f, size * f, size * f), f)
        errors = {}
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                moved = src.read_level(level, x + dx, y + dy, size, size).astype(np.float64)
                errors[dx, dy] = float(np.abs(moved - ref).mean())
        assert min(errors, key=errors.__getitem__) == (0, 0), level
    # a new worker serves the same pixels
    process = src._worker._process
    assert process is not None
    os.kill(process.pid, signal.SIGKILL)
    process.wait()
    np.testing.assert_array_equal(src.read_level(0, 23995, 38533, 512, 512), region)
    src.close()


@pytest.mark.testdata
def test_os2_incomplete_copies_are_refused(tmp_path: Path) -> None:
    """The survey's broken copies, made of links: slideio opens each with 0 scenes."""
    path = sample_path(OS2, tier=3)
    folder = path.parent / "_OS-2_"
    cases = {
        "vsi_only": {},
        "renamed": {"_OS-2_": folder},
        "case": {"_os-2_": folder},
        "no_acquisition": {"_OS-2_/stack1": folder / "stack1"},
        "no_overview": {"_OS-2_/stack10001": folder / "stack10001"},
    }
    for case, links in cases.items():
        root = tmp_path / case
        (root / "_OS-2_").parent.mkdir(parents=True)
        vsi = root / ("other.vsi" if case == "renamed" else "OS-2.vsi")
        vsi.symlink_to(path)
        for rel, target in links.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).symlink_to(target)
        with pytest.raises(SlideNotFound, match="is incomplete"):
            open_source(vsi, MATCH, name="s1")
