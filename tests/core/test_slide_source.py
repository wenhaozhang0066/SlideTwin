from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import InvalidParameter, SlideMetadataError, SlideNotFound
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.slide_source import (
    ArraySlideSource,
    OpenSlideSource,
    TiffSlideSource,
    native_mpp,
    open_slide,
    resolve_mpp,
    slide_size_um,
    source_level_mpps,
    source_level_transforms,
    source_provenance,
    to_rgb,
)
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff


@pytest.fixture(scope="module")
def levels() -> list[np.ndarray]:
    return pyramid(line_image(1024, 768, (100, 333), (50, 600)), 3)


@pytest.fixture(scope="module")
def tiff_path(tmp_path_factory: pytest.TempPathFactory, levels: list[np.ndarray]) -> Path:
    return write_pyramid_tiff(tmp_path_factory.mktemp("tiff") / "syn.tif", levels, 0.25)


def test_array_source_basics(levels: list[np.ndarray]) -> None:
    src = ArraySlideSource(levels, (0.25, 0.3), name="a")
    assert src.dimensions == (1024, 768)
    assert src.level_dimensions == ((1024, 768), (512, 384), (256, 192))
    assert src.mpp == (0.25, 0.3)
    assert src.backend == "array"
    assert native_mpp(src) == 0.3
    assert slide_size_um(src) == pytest.approx((256.0, 230.4))
    assert source_level_mpps(src)[1] == (0.5, 0.6)
    patch = src.read_level(0, 90, 40, 20, 20)
    assert patch.shape == (20, 20, 3)
    assert (patch[:, 10] == 0).all()  # column 100
    patch[:] = 7  # a copy, not a view
    assert src.read_level(0, 90, 40, 20, 20)[0, 0, 0] == 255
    src.close()


@pytest.mark.parametrize(
    "args",
    [(3, 0, 0, 1, 1), (-1, 0, 0, 1, 1), (0, -1, 0, 1, 1), (0, 0, 0, 0, 1), (0, 1020, 0, 10, 1)],
)
def test_reads_outside_level_are_rejected(levels: list[np.ndarray], args: tuple[int, ...]) -> None:
    src = ArraySlideSource(levels, (0.25, 0.25))
    with pytest.raises(InvalidParameter):
        src.read_level(*args)


def test_array_source_rejects_bad_levels() -> None:
    with pytest.raises(SlideMetadataError):
        ArraySlideSource([np.zeros((4, 4), np.uint8)], (1.0, 1.0))
    with pytest.raises(SlideMetadataError):
        ArraySlideSource([], (1.0, 1.0))


@pytest.mark.parametrize(
    ("raw", "override"),
    [
        ((None, None), None),
        (("abc", "1"), None),
        (("-1", "1"), None),
        (("inf", "1"), None),
        (("0", "0"), None),
    ],
)
def test_resolve_mpp_refuses_to_guess(raw: tuple[object, object], override: None) -> None:
    with pytest.raises(SlideMetadataError) as err:
        resolve_mpp(raw[0], raw[1], override, "x.tif")
    assert "refusing to guess" in err.value.message
    assert err.value.hint is not None
    assert "mpp_override" in err.value.hint


def test_resolve_mpp_prefers_override() -> None:
    assert resolve_mpp("0.5", "0.5", (0.24, 0.25), "x") == (0.24, 0.25)
    assert resolve_mpp("0.5", "0.4", None, "x") == (0.5, 0.4)
    with pytest.raises(SlideMetadataError):
        resolve_mpp("0.5", "0.5", (math.nan, 0.25), "x")


def test_to_rgb_layouts() -> None:
    gray = np.array([[0, 128]], np.uint8)
    assert to_rgb(gray).shape == (1, 2, 3)
    rgba = np.array([[[10, 20, 30, 0], [10, 20, 30, 255], [0, 0, 0, 128]]], np.uint8)
    out = to_rgb(rgba)
    assert out[0, 0].tolist() == [255, 255, 255]  # transparent -> white
    assert out[0, 1].tolist() == [10, 20, 30]
    assert out[0, 2].tolist() == [127, 127, 127]
    with pytest.raises(SlideMetadataError):
        to_rgb(np.zeros((2, 2, 3), np.uint16))
    with pytest.raises(SlideMetadataError):
        to_rgb(np.zeros((2, 2, 2), np.uint8))


@pytest.mark.parametrize("backend", ["tiffslide", "openslide"])
def test_file_backends_read_the_pyramid(
    tiff_path: Path, levels: list[np.ndarray], backend: str
) -> None:
    src = open_slide(tiff_path, backend=backend, name="syn")
    assert isinstance(src, TiffSlideSource if backend == "tiffslide" else OpenSlideSource)
    assert src.backend.startswith(backend)
    assert src.dimensions == (1024, 768)
    assert len(src.level_dimensions) == 3
    assert src.mpp == pytest.approx((0.25, 0.25))
    for lvl in range(3):
        h, w = levels[lvl].shape[:2]
        np.testing.assert_array_equal(src.read_level(lvl, 0, 0, w, h), levels[lvl])
    np.testing.assert_array_equal(src.read_level(1, 30, 20, 17, 9), levels[1][20:29, 30:47])
    src.close()


@pytest.mark.parametrize("backend", ["tiffslide", "openslide"])
def test_file_without_mpp_is_refused_unless_overridden(tmp_path: Path, backend: str) -> None:
    path = tmp_path / "nompp.tif"
    tifffile.imwrite(
        path, np.full((256, 256, 3), 200, np.uint8), tile=(256, 256), photometric="rgb"
    )
    with pytest.raises(SlideMetadataError) as err:
        open_slide(path, backend=backend, name="slide_7")
    assert "slide_7" in err.value.message
    assert "nompp" not in err.value.message  # file names never reach messages
    src = open_slide(path, backend=backend, mpp_override=(0.5, 0.5), name="slide_7")
    assert src.mpp == (0.5, 0.5)
    src.close()


def test_open_slide_errors(tmp_path: Path, tiff_path: Path) -> None:
    with pytest.raises(SlideNotFound) as err:
        open_slide(tmp_path / "tumor_001.svs", name="slide_x")
    assert "slide_x" in err.value.message
    assert "tumor" not in err.value.message  # no file names (they may carry labels)
    assert str(tmp_path) not in err.value.message  # no internal paths in messages
    with pytest.raises(InvalidParameter):
        open_slide(tiff_path, backend="bioformats", name="slide_x")
    broken = tmp_path / "tumor_002.tif"
    broken.write_bytes(b"II*\x00" + b"\xff" * 32)
    for backend in ("tiffslide", "openslide"):
        with pytest.raises(SlideMetadataError, match="slide_x") as bad:
            open_slide(broken, backend=backend, name="slide_x")
        assert "tumor" not in bad.value.message


@pytest.mark.testdata
def test_cmu_slide_metadata(cmu_small_path: Path) -> None:
    src = open_slide(cmu_small_path, name="cmu1_small")
    assert src.mpp == (0.499, 0.499)
    assert src.dimensions == (2220, 2967)
    assert len(src.level_dimensions) == 1
    # associated images (label, macro) exist in the file but have no accessor
    assert not hasattr(src, "associated_images")
    src.close()


def test_provenance_keeps_versions_apart(tiff_path: Path, levels: list[np.ndarray]) -> None:
    assert source_provenance(ArraySlideSource(levels, (0.25, 0.25))) == {"reader": "array"}
    src = open_slide(tiff_path, name="syn")
    prov = source_provenance(src)
    assert prov["reader"] == "tiffslide"
    assert set(prov["versions"]) == {"tiffslide"}
    prov["reader"] = "changed"  # a copy: the source keeps its own
    assert source_provenance(src)["reader"] == "tiffslide"
    src.close()


def test_sources_without_the_optional_members_get_defaults() -> None:
    class Minimal:  # a SlideSource written before M8
        dimensions = (400, 200)
        mpp = (0.5, 0.5)
        level_dimensions = ((400, 200), (100, 50))
        backend = "custom 2.1"

    src: Any = Minimal()
    assert source_provenance(src) == {"reader": "custom", "versions": {"custom": "2.1"}}
    assert source_level_transforms(src) == (LevelTransform(1.0, 1.0), LevelTransform(4.0, 4.0))
    assert source_level_mpps(src) == ((0.5, 0.5), (2.0, 2.0))


def test_openslide_backend_without_the_library(
    tiff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "openslide", None)  # as on a base install
    with pytest.raises(SlideMetadataError, match="not installed") as err:
        open_slide(tiff_path, backend="openslide", name="s1")
    assert "slidetwin[openslide]" in (err.value.hint or "")
