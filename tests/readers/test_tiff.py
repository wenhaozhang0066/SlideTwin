"""TIFF family reader (M8): structure gate, trusted mpp, white fill, parity with tiffslide."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.slide_source import ReaderOptions, TiffSlideSource
from slidetwin.readers import open_source
from slidetwin.readers.tiff import TiffFamilySource
from tests.helpers.synth import line_image, pyramid

LEVELS = pyramid(line_image(512, 384, (100, 300), (50,)), 3)
PX_PER_CM = 1e4 / 0.5  # 0.5 um/px


def write_tiff(path: Path, levels: list[np.ndarray] = LEVELS, **kw: Any) -> Path:
    """One top-level page per level; `first` is written before level 0 (e.g. a label)."""
    first = kw.pop("first", None)
    page_kw = {"tile": (64, 64), "photometric": "rgb", "metadata": None}
    res = kw.pop("resolution", (PX_PER_CM, PX_PER_CM))
    unit = kw.pop("resolutionunit", "CENTIMETER")
    with tifffile.TiffWriter(path) as tif:
        if first is not None:
            tif.write(first, **page_kw)
        for i, level in enumerate(levels):
            f = levels[0].shape[1] / level.shape[1]
            level_res = (res[0] / f, res[1] / f) if res else None
            tif.write(
                level,
                subfiletype=0 if i == 0 else 1,
                resolution=level_res,
                resolutionunit=unit if res else None,
                **page_kw,
                **(kw if i == 0 else {}),
            )
    return path


def open_tiff(path: Path, **kw: Any) -> TiffFamilySource:
    return TiffFamilySource(path, name="s1", fmt=sniff(path).format, **kw)


def test_generic_pyramid_reads_exactly(tmp_path: Path) -> None:
    src = open_tiff(write_tiff(tmp_path / "a.tif"))
    assert src.level_dimensions == ((512, 384), (256, 192), (128, 96))
    assert src.mpp == pytest.approx((0.5, 0.5))
    for i, level in enumerate(LEVELS):
        h, w = level.shape[:2]
        np.testing.assert_array_equal(src.read_level(i, 0, 0, w, h), level)
    prov = src.provenance
    assert (prov["reader"], prov["format"], prov["mpp_source"]) == (
        "tifffile",
        "generic-tiff",
        "tiff resolution tags",
    )
    assert set(prov["versions"]) == {"tifffile", "imagecodecs"}
    src.close()


def test_matches_tiffslide_pixel_for_pixel(tmp_path: Path) -> None:
    path = write_tiff(tmp_path / "a.tif")
    new, old = open_tiff(path), TiffSlideSource(path, name="s1")
    assert new.level_dimensions == old.level_dimensions
    assert new.mpp == old.mpp
    for level in range(3):
        np.testing.assert_array_equal(
            new.read_level(level, 7, 5, 90, 60), old.read_level(level, 7, 5, 90, 60)
        )


def test_missing_tiles_are_white(tmp_path: Path) -> None:
    path = write_tiff(tmp_path / "sparse.tif")
    with tifffile.TiffFile(path, mode="r+b") as tif:  # tile 0 of level 0 was never scanned
        counts = tif.pages[0].tags["TileByteCounts"]
        counts.overwrite((0, *counts.value[1:]))
    src = open_tiff(path)
    assert (src.read_level(0, 0, 0, 64, 64) == 255).all()  # tiffslide would give black
    np.testing.assert_array_equal(src.read_level(0, 64, 0, 64, 64), LEVELS[0][:64, 64:128])


def test_gray_is_served_as_rgb(tmp_path: Path) -> None:
    gray = [lv[:, :, 0].copy() for lv in LEVELS]
    path = tmp_path / "gray.tif"
    with tifffile.TiffWriter(path) as tif:
        for i, level in enumerate(gray):
            f = 512 / level.shape[1]
            tif.write(
                level,
                tile=(64, 64),
                photometric="minisblack",
                metadata=None,
                subfiletype=0 if i == 0 else 1,
                resolution=(PX_PER_CM / f, PX_PER_CM / f),
                resolutionunit="CENTIMETER",
            )
    rgb = open_tiff(path).read_level(1, 0, 0, 10, 10)
    assert rgb.shape == (10, 10, 3)
    assert (rgb[..., 0] == gray[1][:10, :10]).all()


@pytest.mark.parametrize(
    "kw",
    [
        {"resolution": None},  # no resolution tags
        {"resolutionunit": "NONE"},
        {"resolution": (10.0, 10.0)},  # the 10 px/cm placeholder: 1000 um/px
        {"resolution": (28.35, 28.35)},  # 72 dpi
    ],
)
def test_untrusted_mpp_needs_an_override(tmp_path: Path, kw: dict[str, Any]) -> None:
    path = write_tiff(tmp_path / "a.tif", **kw)
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_tiff(path)
    src = open_tiff(path, mpp_override=(0.25, 0.25))
    assert src.mpp == (0.25, 0.25)
    assert src.provenance["mpp_source"] == "mpp_override"


def test_inch_resolution(tmp_path: Path) -> None:
    dpi = 25_400 / 0.5
    src = open_tiff(write_tiff(tmp_path / "a.tif", resolution=(dpi, dpi), resolutionunit="INCH"))
    assert src.mpp == pytest.approx((0.5, 0.5))


def test_label_before_the_slide_is_refused(tmp_path: Path) -> None:
    label = np.zeros((100, 200, 3), np.uint8)  # a label stored before the slide
    path = write_tiff(tmp_path / "a.tif", first=label)
    with pytest.raises(SlideMetadataError, match="not one clean pyramid"):
        open_tiff(path)


def test_pages_with_other_proportions_are_not_levels(tmp_path: Path) -> None:
    odd = [LEVELS[0], np.zeros((150, 256, 3), np.uint8)]  # 256 wide but 150 instead of 192 high
    src = open_tiff(write_tiff(tmp_path / "a.tif", levels=odd))
    assert src.level_dimensions == ((512, 384),)  # another series, never served


def test_only_8_bit_data_is_served(tmp_path: Path) -> None:
    path = tmp_path / "u16.tif"
    tifffile.imwrite(
        path,
        np.zeros((128, 128, 3), np.uint16),
        tile=(64, 64),
        photometric="rgb",
        resolution=(PX_PER_CM, PX_PER_CM),
        resolutionunit="CENTIMETER",
        metadata=None,
    )
    with pytest.raises(SlideMetadataError, match="8-bit"):
        open_tiff(path)
    two = tmp_path / "two.tif"
    tifffile.imwrite(
        two,
        np.zeros((128, 128, 2), np.uint8),
        tile=(64, 64),
        planarconfig="contig",
        photometric="minisblack",
        resolution=(PX_PER_CM, PX_PER_CM),
        resolutionunit="CENTIMETER",
        metadata=None,
    )
    with pytest.raises(SlideMetadataError):
        open_tiff(two)


def test_svs_mpp_from_the_description(tmp_path: Path) -> None:
    desc = (
        "Aperio Image Library v12.0.15 \n512x384 [0,0 512x384] (64x64) JPEG/RGB Q=70|MPP = 0.2520"
    )
    src = open_tiff(write_tiff(tmp_path / "a.svs", description=desc, resolution=None))
    assert src.mpp == (0.252, 0.252)
    assert src.provenance["mpp_source"] == "svs MPP"
    no_mpp = write_tiff(tmp_path / "b.svs", description="Aperio Image Library v12 \nno resolution")
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_tiff(no_mpp)


PHILIPS = (
    '<?xml version="1.0" encoding="UTF-8" ?><DataObject ObjectType="DPUfsImport">'
    '<Attribute Name="DICOM_MANUFACTURER">Hamamatsu</Attribute>'
    '<Attribute Name="DICOM_PIXEL_SPACING">"0.000226316" "0.000226321"</Attribute>'
    '<Attribute Name="DICOM_PIXEL_SPACING">"0.000452632" "0.000452642"</Attribute>'
    "</DataObject>"
)


def test_philips_mpp_is_row_then_column(tmp_path: Path) -> None:
    path = write_tiff(tmp_path / "p.tif", description=PHILIPS, software="Philips DP v1.0")
    src = open_tiff(path)
    assert src.provenance["format"] == "philips-tiff"
    assert src.mpp == pytest.approx((0.226321, 0.226316))


def test_philips_without_spacing_is_refused(tmp_path: Path) -> None:
    bare = '<?xml version="1.0" ?><DataObject ObjectType="DPUfsImport"></DataObject>'
    path = write_tiff(tmp_path / "p.tif", description=bare, software="Philips DP v1.0")
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_tiff(path)


def test_zero_resolution_is_no_resolution(tmp_path: Path) -> None:
    path = write_tiff(tmp_path / "z.tif")
    with tifffile.TiffFile(path, mode="r+b") as tif:
        tif.pages[0].tags["XResolution"].overwrite((0, 1))
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_tiff(path)


def test_huron_mpp_keeps_both_axes(tmp_path: Path) -> None:
    path = write_tiff(
        tmp_path / "h.tif",
        resolution=(25_000.0, 20_000.0),
        extratags=[(271, "s", 0, "Huron", True)],
    )
    src = open_tiff(path)
    assert src.provenance["format"] == "huron-tiff"
    assert src.mpp == pytest.approx((0.4, 0.5))


def test_damaged_files_and_reads(tmp_path: Path) -> None:
    for content in (b"II*\x00" + b"\x00" * 16, b"not a tiff at all"):
        junk = tmp_path / "junk.tif"
        junk.write_bytes(content)
        with pytest.raises(SlideMetadataError, match="s1"):
            TiffFamilySource(junk, name="s1", fmt="generic-tiff")
    with pytest.raises(SlideMetadataError, match="does not handle"):
        TiffFamilySource(junk, name="s1", fmt="zeiss-czi")
    src = open_tiff(write_tiff(tmp_path / "a.tif"))
    src._arrays[0] = None  # a decoder failure inside a read
    with pytest.raises(SlideReadError, match="s1"):
        src.read_level(0, 0, 0, 8, 8)


def test_open_source_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_tiff(tmp_path / "a.tif")
    assert isinstance(
        open_source(path, FormatMatch("generic-tiff", "tifffile"), name="s1"), TiffFamilySource
    )
    with pytest.raises(SlideMetadataError, match="cannot read"):
        open_source(path, FormatMatch("plain-image", None, "Do X."), name="s1")
    with pytest.raises(SlideMetadataError, match="does not have yet"):
        open_source(path, FormatMatch("future-format", "future-reader"), name="s1")
    import slidetwin.readers as readers

    monkeypatch.setitem(readers.READERS, "openslide", ("slidetwin_no_such_reader", "openslide"))
    with pytest.raises(SlideMetadataError, match="is not installed") as err:
        open_source(path, FormatMatch("hamamatsu-ndpi", "openslide"), name="s1")
    assert "slidetwin[openslide]" in (err.value.hint or "")


@pytest.mark.testdata
def test_cmu_svs_matches_tiffslide(cmu_small_path: Path) -> None:
    new, old = open_tiff(cmu_small_path), TiffSlideSource(cmu_small_path, name="s1")
    assert (new.mpp, new.level_dimensions) == (old.mpp, old.level_dimensions)
    np.testing.assert_array_equal(
        new.read_level(0, 100, 200, 300, 250), old.read_level(0, 100, 200, 300, 250)
    )


def test_tiff_formats_have_no_scenes_planes_or_channels(tmp_path: Path) -> None:
    path = write_tiff(tmp_path / "a.tif")
    match = FormatMatch("generic-tiff", "tifffile")
    with pytest.raises(SlideMetadataError, match="has no scene or plane to choose"):
        open_source(path, match, name="s1", options=ReaderOptions(scene=1, plane=0))
