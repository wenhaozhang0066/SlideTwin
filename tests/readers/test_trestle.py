"""Trestle TIFF reader (M8): grid stitching, later tile wins, coarse-level offset, um/px tags."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import BBox, LevelTransform
from slidetwin.core.render import render_region
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.trestle import TrestleSource, open_reader, overlaps
from tests.conftest import sample_path
from tests.helpers.make_trestle import SOFTWARE, box, write_trestle

MATCH = FormatMatch("trestle-tiff", "trestle")


def open_trestle(path: Path, **kw: Any) -> TrestleSource:
    kw.setdefault("mpp_override", None)
    options = kw.pop("options", ReaderOptions())
    return open_reader(path, MATCH, name="s1", options=options, **kw)


def full(src: TrestleSource, level: int) -> np.ndarray:
    w, h = src.level_dimensions[level]
    return src.read_level(level, 0, 0, w, h)


def test_stitched_levels_and_their_geometry(tmp_path: Path) -> None:
    f = write_trestle(tmp_path / "t.tif")
    assert sniff(f.path) == MATCH
    src = open_source(f.path, sniff(f.path), name="s1")
    assert isinstance(src, TrestleSource)
    assert src.level_dimensions == ((416, 224), (208, 112), (96, 48), (48, 24))
    assert src.level_transforms == (
        LevelTransform(1.0, 1.0),
        LevelTransform(2.0, 2.0),
        LevelTransform(4.0, 4.0, 16.0, 16.0),  # half the level-0 overlap of 32
        LevelTransform(8.0, 8.0, 16.0, 16.0),
    )
    assert src.mpp == pytest.approx((0.5, 0.5))
    for k in range(4):  # the green strips that later tiles cover never show
        np.testing.assert_array_equal(full(src, k), f.stitched[k])
    rng = np.random.default_rng(0)
    for _ in range(20):  # windows anywhere, across tile corners
        x, y = int(rng.integers(0, 380)), int(rng.integers(0, 190))
        np.testing.assert_array_equal(
            src.read_level(0, x, y, 37, 29), f.stitched[0][y : y + 29, x : x + 37]
        )
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"]) == ("trestle-tiff", "trestle XResolution as um/px")
    assert prov["stitcher"].startswith("grid of tile - overlap, later tile wins")
    src.close()


def test_coarse_levels_render_in_place(tmp_path: Path) -> None:
    """At 2 um/px the renderer reads coarse level 2. With its offset, the view equals level 0
    averaged over the same rectangle; without it, it would be 4 level-2 pixels off."""
    f = write_trestle(tmp_path / "t.tif")
    src = open_trestle(f.path)
    x0, y0 = 16 + 4 * 10, 16 + 4 * 5  # level-0 px on the level-2 grid
    bbox = BBox(x0 * 0.5, y0 * 0.5, (x0 + 160) * 0.5, (y0 + 96) * 0.5)
    view = render_region(src, bbox, (40, 24)).astype(int)
    expected = box(f.stitched[0][y0 : y0 + 96, x0 : x0 + 160], 4).astype(int)
    assert np.abs(view - expected).max() <= 1


def test_overlapped_levels_match_openslide(tmp_path: Path) -> None:
    openslide = pytest.importorskip("openslide")
    f = write_trestle(tmp_path / "t.tif")
    ref = openslide.OpenSlide(str(f.path))
    assert ref.properties["openslide.vendor"] == "trestle"
    src = open_trestle(f.path)
    for k in (0, 1):
        w, h = src.level_dimensions[k]
        expected = np.asarray(ref.read_region((0, 0), k, (w, h)))[:, :, :3]
        np.testing.assert_array_equal(full(src, k), expected)


def test_overlaps_from_the_description() -> None:
    assert overlaps("Objective Power=10;OverlapsXY= 64 64 32 32 16 16") == [
        (64, 64),
        (32, 32),
        (16, 16),
    ]
    assert overlaps("OverlapsXY= 64 64 32") == []
    assert overlaps("Objective Power=10") == []


@pytest.mark.parametrize(
    "kw",
    [
        {"description": "Objective Power=10"},  # no overlaps recorded
        {"description": "OverlapsXY= 32 32 0 0"},  # a zero overlap
        {"description": "OverlapsXY= 80 80 40 40"},  # overlaps as large as the tiles
        {"description": "OverlapsXY= 32 32 16 16 8 8 4 4 2 2"},  # more levels than pages
        {"description": "OverlapsXY= 32 32 16 16 16 16"},  # level 2 not stitched to 2^k
        {"coarse_margin": 0},  # coarse levels that span the whole image: not seen on CMU-1
    ],
)
def test_structures_not_seen_on_cmu1_are_refused(tmp_path: Path, kw: dict[str, Any]) -> None:
    path = write_trestle(tmp_path / "t.tif", **kw).path
    with pytest.raises(SlideMetadataError, match="layout SlideTwin validated"):
        open_trestle(path)


def test_partial_or_untiled_pages_are_refused(tmp_path: Path) -> None:
    desc = "OverlapsXY= 32 32"
    partial = tmp_path / "p.tif"
    with tifffile.TiffWriter(partial) as tw:  # 100 px is not a whole number of 64-px tiles
        tw.write(
            np.zeros((64, 100, 3), np.uint8),
            tile=(64, 64),
            description=desc,
            software=SOFTWARE,
            photometric="rgb",
            metadata=None,
        )
    untiled = tmp_path / "u.tif"
    with tifffile.TiffWriter(untiled) as tw:
        tw.write(
            np.zeros((64, 128, 3), np.uint8),
            tile=(64, 64),
            description=desc,
            software=SOFTWARE,
            photometric="rgb",
            metadata=None,
        )
        tw.write(np.zeros((16, 48, 3), np.uint8), subfiletype=1, photometric="rgb", metadata=None)
    for path in (partial, untiled):
        assert sniff(path) == MATCH
        with pytest.raises(SlideMetadataError, match="layout SlideTwin validated"):
            open_trestle(path)


@pytest.mark.parametrize(
    "kw",
    [
        {"level_res_scale": 0.5},  # values that halve per level: not um/px
        {"um_per_px": (50.0, 50.0)},  # implausible
    ],
)
def test_untrusted_resolution_needs_an_override(tmp_path: Path, kw: dict[str, Any]) -> None:
    path = write_trestle(tmp_path / "t.tif", **kw).path
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_trestle(path)
    src = open_trestle(path, mpp_override=(0.5, 0.5))
    assert src.provenance["mpp_source"] == "mpp_override"


def test_missing_resolution_tags(tmp_path: Path) -> None:
    path = write_trestle(tmp_path / "t.tif").path
    with tifffile.TiffFile(path, mode="r+b") as tif:
        tif.pages[2].tags["XResolution"].overwrite((1, 0))  # a level without a usable value
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_trestle(path)


def test_no_scene_plane_or_channels(tmp_path: Path) -> None:
    path = write_trestle(tmp_path / "t.tif").path
    with pytest.raises(SlideMetadataError, match="has no scene to choose"):
        open_trestle(path, options=ReaderOptions(scene=0))


def test_provenance_has_no_names(tmp_path: Path) -> None:
    src = open_trestle(write_trestle(tmp_path / "t.tif").path)
    assert "MedScan" not in json.dumps(src.provenance)


# --- the CC0 sample (tier 3, local) ----------------------------------------------------------

TISSUE = [
    (24560, 2725),
    (28215, 8015),
    (14748, 19654),
    (11478, 21193),
    (5899, 15325),
    (33120, 7727),
]


@pytest.fixture(scope="module")
def cmu1() -> Path:
    return sample_path("Trestle/CMU-1/CMU-1.tif", tier=3)


@pytest.mark.testdata
def test_cmu1_levels_and_mpp(cmu1: Path) -> None:
    src = open_trestle(cmu1)
    assert src.level_dimensions == (
        (40000, 27712),
        (20000, 13856),
        (10000, 6928),
        (4992, 3456),
        (2496, 1728),
        (1248, 864),
        (624, 432),
    )
    assert src.level_transforms[3] == LevelTransform(8.0, 8.0, 32.0, 32.0)
    assert src.mpp == pytest.approx((0.574692, 0.575062), rel=1e-5)


@pytest.mark.testdata
def test_cmu1_stitched_levels_match_openslide(cmu1: Path) -> None:
    openslide = pytest.importorskip("openslide")
    src, ref = open_trestle(cmu1), openslide.OpenSlide(str(cmu1))
    for k in (0, 1, 2):
        lw, lh = src.level_dimensions[k]
        points = [(x >> k, y >> k) for x, y in TISSUE] + [(lw - 512, lh - 512)]
        for x, y in points:
            x, y = min(x, lw - 512), min(y, lh - 512)
            expected = np.asarray(ref.read_region((x << k, y << k), k, (512, 512)))[:, :, :3]
            np.testing.assert_array_equal(src.read_level(k, x, y, 512, 512), expected)


@pytest.mark.testdata
def test_cmu1_coarse_levels_sit_at_half_the_overlap(cmu1: Path) -> None:
    """Level k averaged from level 0 at X0 = 32 + 2^k X matches level k at X with no shift."""
    src = open_trestle(cmu1)
    for k in (3, 4):
        f, n, r = 2**k, 48, 3
        for x0, y0 in TISSUE:
            xk, yk = (x0 - 32) // f, (y0 - 32) // f
            ref = box(src.read_level(0, 32 + f * xk, 32 + f * yk, n * f, n * f), f).mean(axis=2)
            mov = src.read_level(k, xk - r, yk - r, n + 2 * r, n + 2 * r).astype(float).mean(axis=2)
            errors = {
                (dx, dy): np.abs(ref - mov[r + dy : r + dy + n, r + dx : r + dx + n]).mean()
                for dx in range(-r, r + 1)
                for dy in range(-r, r + 1)
            }
            assert min(errors, key=errors.__getitem__) == (0, 0), (k, x0, y0)
