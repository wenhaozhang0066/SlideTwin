"""ARGOS AVS reader (M8): the Baseline pyramid, 2^k levels, white missing tiles, focal planes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.slide_source import OpenSlideSource, ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.argos import open_reader
from slidetwin.readers.tiffpages import TiffPagesSource
from tests.conftest import sample_path
from tests.helpers.make_argos import TILE, XML, write_argos

MATCH = FormatMatch("argos-avs", "argos")


def open_argos(path: Path, **kw: Any) -> TiffPagesSource:
    kw.setdefault("mpp_override", None)
    options = kw.pop("options", ReaderOptions())
    return open_reader(path, MATCH, name="s1", options=options, **kw)


def full(src: TiffPagesSource, level: int) -> np.ndarray:
    w, h = src.level_dimensions[level]
    return src.read_level(level, 0, 0, w, h)


def test_single_plane_pyramid(tmp_path: Path) -> None:
    f = write_argos(tmp_path / "a.avs")
    assert sniff(f.path) == MATCH
    src = open_source(f.path, sniff(f.path), name="s1")
    assert isinstance(src, TiffPagesSource)
    assert src.level_dimensions == ((1100, 700), (550, 350), (275, 175))  # Map, Macro not levels
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(3))
    assert src.mpp == pytest.approx((0.25, 0.25))
    for k in range(3):
        np.testing.assert_array_equal(full(src, k), f.truth[0][k])
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"], prov["fill"]) == (
        "argos-avs",
        "tiff resolution tags",
        [255, 255, 255],
    )
    assert "plane" not in prov
    assert "SECRET" not in json.dumps(prov)
    src.close()


def test_missing_tiles_are_white(tmp_path: Path) -> None:
    src = open_argos(write_argos(tmp_path / "a.avs").path)
    assert (src.read_level(0, TILE, 0, TILE, TILE) == 255).all()  # tile (1, 0) was dropped
    assert (src.read_level(1, TILE, TILE, 20, 20) == 255).all()
    assert not (src.read_level(0, 0, 0, TILE, TILE) == 255).all()


def test_the_pyramid_does_not_depend_on_map_and_macro(tmp_path: Path) -> None:
    f = write_argos(tmp_path / "a.avs", associated=False)
    src = open_argos(f.path)
    np.testing.assert_array_equal(full(src, 2), f.truth[0][2])


def test_z_stack_shows_the_middle_plane_by_default(tmp_path: Path) -> None:
    f = write_argos(tmp_path / "z.avs", planes=3)
    src = open_argos(f.path)
    np.testing.assert_array_equal(full(src, 0), f.truth[1][0])
    assert {k: src.provenance[k] for k in ("plane", "planes", "plane_choice")} == {
        "plane": 1,
        "planes": 3,
        "plane_choice": "middle (default; z order not verified)",
    }
    for plane in (0, 2):
        chosen = open_argos(f.path, options=ReaderOptions(plane=plane))
        for k in range(3):
            np.testing.assert_array_equal(full(chosen, k), f.truth[plane][k])
        assert chosen.provenance["plane_choice"] == "manifest"
    with pytest.raises(SlideMetadataError, match="has no plane 3; it has 3"):
        open_argos(f.path, options=ReaderOptions(plane=3))


def test_middle_plane_is_openslides(tmp_path: Path) -> None:
    openslide = pytest.importorskip("openslide")
    f = write_argos(tmp_path / "z.avs", planes=3)
    ref = openslide.OpenSlide(str(f.path))
    expected = np.asarray(ref.read_region((0, 0), 0, (1100, 700)))[:, :, :3]
    alpha = np.asarray(ref.read_region((0, 0), 0, (1100, 700)))[:, :, 3]
    ours = full(open_argos(f.path), 0)
    np.testing.assert_array_equal(ours[alpha == 255], expected[alpha == 255])
    assert (ours[alpha == 0] == 255).all()  # OpenSlide leaves missing tiles transparent


def test_options_a_single_plane_file_can_take(tmp_path: Path) -> None:
    path = write_argos(tmp_path / "a.avs").path
    assert "plane" not in open_argos(path, options=ReaderOptions(plane=0)).provenance
    with pytest.raises(SlideMetadataError, match="has no plane 1"):
        open_argos(path, options=ReaderOptions(plane=1))
    with pytest.raises(SlideMetadataError, match="has no scene or channels to choose"):
        open_argos(path, options=ReaderOptions(scene=0, channels=(0,)))


def test_mpp_needs_resolution_tags(tmp_path: Path) -> None:
    path = tmp_path / "a.avs"
    write_argos(path)
    with tifffile.TiffFile(path, mode="r+b") as tif:
        tif.pages[0].tags["ResolutionUnit"].overwrite(1)  # NONE: no physical unit
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_argos(path)
    src = open_argos(path, mpp_override=(0.3, 0.3))
    assert (src.mpp, src.provenance["mpp_source"]) == ((0.3, 0.3), "mpp_override")


def _write_layout(path: Path, levels: list[np.ndarray]) -> Path:
    xml = XML.format(zrange=0, minz=0, maxz=0)
    with tifffile.TiffWriter(path, bigtiff=True) as tw:
        for k, level in enumerate(levels):
            tw.write(
                level,
                tile=(64, 64),
                photometric="rgb" if level.ndim == 3 else "minisblack",
                metadata=None,
                software=False,
                resolution=(4e4 / 2**k, 4e4 / 2**k),
                resolutionunit="CENTIMETER",
                extratags=[(65000, "s", 0, xml, True)] if k == 0 else None,
            )
    return path


@pytest.mark.parametrize(
    "levels",
    [
        [np.zeros((200, 300, 3), np.uint8), np.zeros((100, 149, 3), np.uint8)],  # not floor(w/2)
        [np.zeros((200, 300), np.uint8), np.zeros((100, 150), np.uint8)],  # grey
    ],
)
def test_other_layouts_are_refused(tmp_path: Path, levels: list[np.ndarray]) -> None:
    path = _write_layout(tmp_path / "x.avs", levels)
    assert sniff(path).format == "argos-avs"
    with pytest.raises(SlideMetadataError, match="layout SlideTwin validated"):
        open_argos(path)


def test_damaged_files_and_reads(tmp_path: Path) -> None:
    junk = tmp_path / "junk.avs"
    junk.write_bytes(b"II+\x00" + b"\x00" * 12)
    with pytest.raises(SlideMetadataError, match="'s1' could not be opened"):
        open_argos(junk)
    src = open_argos(write_argos(tmp_path / "a.avs").path)
    src._arrays[0] = None  # a decoder failure inside a read
    with pytest.raises(SlideReadError, match="s1"):
        src.read_level(0, 0, 0, 8, 8)


def test_unexpected_errors_name_only_the_type(tmp_path: Path) -> None:
    path = write_argos(tmp_path / "a.avs").path
    with tifffile.TiffFile(path, mode="r+b") as tif:  # tile offsets past the end of the file
        page = tif.pages[0]
        page.tags["TileOffsets"].overwrite([2**40] * len(page.tags["TileOffsets"].value))
    src = open_argos(path)
    with pytest.raises(SlideReadError, match=r"could not be decoded at level 0 \(\w+\)"):
        src.read_level(0, 0, 0, 8, 8)


# --- the CC0 sample (tier 2, CI) -------------------------------------------------------------


@pytest.fixture(scope="module")
def argos1() -> Path:
    return sample_path("Argos/Argos-1.avs", tier=2)


@pytest.mark.testdata
def test_argos1_levels_mpp_and_fill(argos1: Path) -> None:
    src = open_argos(argos1)
    assert src.level_dimensions[0] == (130546, 57440)
    assert src.level_dimensions[7] == (1019, 448)
    assert src.level_transforms[7] == LevelTransform(128.0, 128.0)
    assert src.mpp == pytest.approx((0.38300410109301325,) * 2)
    tissue = src.read_level(0, 82944, 12288, 512, 512).reshape(-1, 3).mean(axis=0)
    assert tissue == pytest.approx((216.03, 137.88, 196.41), abs=0.01)  # survey value
    assert (src.read_level(0, 512, 29184, 512, 512) == 255).all()  # missing tiles
    assert (src.read_level(6, 0, 512, 64, 64) == 255).all()


@pytest.mark.testdata
def test_argos1_matches_openslide(argos1: Path) -> None:
    pytest.importorskip("openslide")
    ours, ref = open_argos(argos1), OpenSlideSource(argos1, name="s1")
    assert ours.level_dimensions == ref.level_dimensions
    assert ours.mpp == pytest.approx(ref.mpp)
    for level in range(8):
        x, y = 82944 >> level, 12288 >> level
        size = min(256, ours.level_dimensions[level][1] - y)
        a = ours.read_level(level, x, y, size, size).astype(int)
        b = ref.read_level(level, x, y, size, size).astype(int)
        diff = np.abs(a - b)
        if level in (0, 1):  # OpenSlide's downsample is exact here: identical pixels
            assert diff.max() == 0
        else:  # OpenSlide resamples levels whose downsample is not an integer
            assert diff.mean() <= 1.5, level
