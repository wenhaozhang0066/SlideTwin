"""DICOM WSI reader (M8): series choice, codec gate, sparse fill, privacy, parity with OpenSlide."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pydicom
import pytest
import wsidicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import SecondaryCaptureImageStorage, generate_uid

import slidetwin.readers.dicom as dicom_reader
from slidetwin.core.errors import SlideIntegrityError, SlideMetadataError, SlideReadError
from slidetwin.core.fileset import fileset_sha256
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.registry import SlideEntry, SlideRegistry, file_sha256
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.dicom import DicomSource, open_reader
from tests.conftest import sample_path
from tests.helpers.dicom_wsi import (
    BARCODE,
    CODECS,
    MAGENTA,
    PATIENT_ID,
    PATIENT_NAME,
    read_header,
    write_series,
)
from tests.helpers.synth import pyramid

MATCH = FormatMatch("dicom", "wsidicom")


def smooth(width: int = 300, height: int = 200) -> np.ndarray:
    """A colourful image without sharp edges, so lossy codecs stay close to it."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    r = 128 + 100 * np.sin(xx / 37.0)
    g = 128 + 100 * np.cos(yy / 29.0)
    b = 128 + 60 * np.sin((xx + yy) / 53.0)
    return np.stack([r, g, b], -1).round().astype(np.uint8)


LEVELS = pyramid(smooth(), 3)  # 300x200, 150x100, 75x50


def mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def open_dicom(path: Path, **kw: Any) -> DicomSource:
    return DicomSource(path, name="s1", **kw)


def test_jpeg_series_levels_mpp_and_provenance(tmp_path: Path) -> None:
    series = write_series(tmp_path / "s", LEVELS, mpp=(0.5, 0.5))
    src = open_dicom(tmp_path / "s")
    assert src.level_dimensions == ((300, 200), (150, 100), (75, 50))
    assert src.mpp == pytest.approx((0.5, 0.5))
    assert src.level_transforms == (
        LevelTransform(1.0, 1.0),
        LevelTransform(2.0, 2.0),
        LevelTransform(4.0, 4.0),
    )
    for i, level in enumerate(LEVELS):
        h, w = level.shape[:2]
        assert mad(src.read_level(i, 0, 0, w, h), level) < 2.0  # JPEG 4:2:2, quality 95
    assert src.fileset == (tmp_path / "s", tuple(sorted(p.name for p in series.levels)))
    prov = src.provenance
    assert prov["reader"] == "wsidicom"
    assert (prov["format"], prov["mpp_source"], prov["fill"], prov["decoder"]) == (
        "dicom",
        "dicom PixelSpacing",
        [255, 255, 255],
        "imagecodecs",
    )
    assert set(prov["versions"]) == {"wsidicom", "pydicom", "imagecodecs"}
    src.close()


def test_only_volume_instances_of_the_series_are_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    series = write_series(tmp_path / "s", LEVELS, others=("LABEL", "OVERVIEW", "THUMBNAIL"))
    (tmp_path / "s" / ".archive-sha256").write_text("not dicom\n")  # ignored
    opened: list[list[Path]] = []
    real_open = wsidicom.WsiDicom.open

    def spy(files: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append([Path(f) for f in files])
        return real_open(files, *args, **kwargs)

    monkeypatch.setattr(wsidicom.WsiDicom, "open", spy)
    src = open_dicom(tmp_path / "s")
    assert sorted(opened[0]) == sorted(series.levels)
    assert src._wsi.labels is None
    assert src._wsi.overviews is None
    assert not set(src.fileset[1]) & {p.name for p in series.others}
    for i, (w, h) in enumerate(src.level_dimensions):
        assert not (src.read_level(i, 0, 0, w, h) == MAGENTA).all(axis=-1).any()


def test_a_level_file_names_its_series(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    mine = write_series(folder, LEVELS, names=["a0.dcm", "a1.dcm", "a2.dcm"])
    write_series(folder, LEVELS[:2], others=(), names=["b0.dcm", "b1.dcm"])  # another series
    src = open_dicom(folder / "a1.dcm")
    assert src.fileset == (folder, tuple(p.name for p in mine.levels))
    assert src.level_dimensions[0] == (300, 200)
    with pytest.raises(SlideMetadataError, match="folder with 2 DICOM whole-slide series"):
        open_dicom(folder)


def test_the_entry_must_be_a_pyramid_level(tmp_path: Path) -> None:
    series = write_series(tmp_path / "s", LEVELS)
    for entry in (series.others[0], _secondary_capture(tmp_path / "s" / "sc.dcm")):
        with pytest.raises(SlideMetadataError, match="not a whole-slide pyramid level"):
            open_dicom(entry)
    labels_only = tmp_path / "labels"
    labels_only.mkdir()
    shutil.copy(series.others[0], labels_only / "label.dcm")
    with pytest.raises(SlideMetadataError, match="folder with 0 DICOM whole-slide series"):
        open_dicom(labels_only)


def _secondary_capture(path: Path) -> Path:
    """A DICOM file of another SOP class (like a DICOMDIR or a screenshot)."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientName = PATIENT_NAME
    ds.save_as(path, enforce_file_format=True)
    return path


def test_no_volume_instances(tmp_path: Path) -> None:
    with pytest.raises(SlideMetadataError, match="no DICOM whole-slide pyramid levels"):
        dicom_reader._check_codecs([], "s1", experimental=False)


def test_damaged_headers_are_refused(tmp_path: Path) -> None:
    series = write_series(tmp_path / "s", LEVELS)
    junk = tmp_path / "s" / "junk.dcm"  # a sequence that runs past the end of the file
    meta = b"\x02\x00\x10\x00UI\x14\x00" + b"1.2.840.10008.1.2.1\x00"
    sequence = b"\x08\x00\x15\x11SQ\x00\x00\xff\xff\xff\xff" + b"\xfe\xff\x00\xe0\xff\xff\xff\xff"
    junk.write_bytes(b"\x00" * 128 + b"DICM" + meta + sequence + b"\x08\x00")
    with pytest.raises(SlideMetadataError, match="header cannot be read"):
        open_dicom(tmp_path / "s")
    junk.unlink()
    ds = pydicom.dcmread(series.levels[1])
    del ds.SamplesPerPixel  # a VOLUME instance without a required field
    ds.save_as(series.levels[1])
    with pytest.raises(SlideMetadataError, match=r"header cannot be read \(AttributeError\)"):
        open_dicom(tmp_path / "s")


@pytest.mark.parametrize(
    "codec",
    [
        "jpeg-444",
        "jpeg-rgb",
        "j2k-ict",
        "j2k-rgb",
        "j2k-lossless-rct",
        "j2k-lossless-rgb",
        "htj2k",
        "htj2k-lossless",
        "jpegxl",
        "jpegxl-lossless",
        "native",
    ],
)
def test_codecs_checked_on_synthetic_files_need_experimental(tmp_path: Path, codec: str) -> None:
    write_series(tmp_path / "s", LEVELS, codec=codec)
    with pytest.raises(SlideMetadataError, match="synthetic files only") as err:
        open_dicom(tmp_path / "s")
    assert "experimental: true" in (err.value.hint or "")
    src = open_dicom(tmp_path / "s", experimental=True)
    assert src.provenance["decoder"] == ("pydicom" if codec == "native" else "imagecodecs")
    limit = 2.0 if CODECS[codec].lossy else 0.0
    for i, level in enumerate(LEVELS):
        h, w = level.shape[:2]
        assert mad(src.read_level(i, 0, 0, w, h), level) <= limit
    src.close()


@pytest.mark.parametrize("codec", ["j2k-ybr-full", "jpegls", "rle", "jpegxl-jpeg"])
def test_other_codecs_are_refused(tmp_path: Path, codec: str) -> None:
    write_series(tmp_path / "s", LEVELS, codec=codec)
    with pytest.raises(SlideMetadataError, match="which SlideTwin does not read"):
        open_dicom(tmp_path / "s", experimental=True)


def test_codec_labels_never_echo_the_file() -> None:
    inst = dicom_reader._Instance("f", "1.2", "1.2.3.4", PATIENT_NAME, 3, (8, 8, 0), (64, 64))
    label = dicom_reader._codec_label(inst)
    assert label == "an unknown transfer syntax with unknown colours"
    assert PATIENT_NAME not in label


def test_sparse_series_fills_missing_tiles_white(tmp_path: Path) -> None:
    gone = {(0, 0, 0), (0, 1, 2), (0, 3, 4), (1, 1, 1)}  # (level, tile row, tile column)
    write_series(
        tmp_path / "s", LEVELS, codec="j2k-lossless-rgb", tile=32, sparse=True, missing=gone
    )
    src = open_dicom(tmp_path / "s", experimental=True)
    level0 = src.read_level(0, 0, 0, 300, 200)
    for lv, r, c in gone:
        tile = src.read_level(lv, c * 32, r * 32, 32, min(32, src.level_dimensions[lv][1] - r * 32))
        assert (tile == 255).all()
    np.testing.assert_array_equal(level0[32:64, 32:64], LEVELS[0][32:64, 32:64])  # present
    edge = src.read_level(0, 288, 192, 12, 8)  # a partial edge tile: its padding never shows
    np.testing.assert_array_equal(edge, LEVELS[0][192:200, 288:300])


def test_non_rgb_and_wide_samples_are_refused(tmp_path: Path) -> None:
    series = write_series(tmp_path / "s", LEVELS)
    ds = pydicom.dcmread(series.levels[0])
    ds.BitsStored = 12
    ds.save_as(series.levels[0])
    with pytest.raises(SlideMetadataError, match="12-bit pixels; SlideTwin reads 8-bit RGB"):
        open_dicom(tmp_path / "s")


def test_levels_must_share_tile_size_and_storage(tmp_path: Path) -> None:
    a = write_series(tmp_path / "a", LEVELS, tile=64)
    b = write_series(tmp_path / "b", LEVELS, tile=32, series_uid=a.series_uid)
    shutil.copy(b.levels[2], a.levels[2])  # level 2 now has 32 px tiles
    with pytest.raises(SlideMetadataError, match="different tile sizes"):
        open_dicom(tmp_path / "a")
    c = write_series(tmp_path / "c", LEVELS, codec="native", series_uid=a.series_uid)
    d = write_series(tmp_path / "d", LEVELS, series_uid=a.series_uid)
    shutil.copy(c.levels[2], d.levels[2])  # level 2 uncompressed, the others JPEG
    with pytest.raises(SlideMetadataError, match="mixes uncompressed and compressed levels"):
        open_dicom(tmp_path / "d", experimental=True)


def test_non_square_pixels_keep_row_and_column_order(tmp_path: Path) -> None:
    write_series(tmp_path / "s", LEVELS, mpp=(0.25, 0.5))
    src = open_dicom(tmp_path / "s")
    assert src.mpp == pytest.approx((0.25, 0.5))  # x from the column spacing, y from the row


def test_implausible_mpp_needs_an_override(tmp_path: Path) -> None:
    write_series(tmp_path / "s", LEVELS, mpp=(100.0, 100.0))
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_dicom(tmp_path / "s")
    src = open_dicom(tmp_path / "s", mpp_override=(0.5, 0.5))
    assert src.mpp == (0.5, 0.5)
    assert src.provenance["mpp_source"] == "mpp_override"


def test_open_and_read_failures_name_only_the_slide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    series = write_series(tmp_path / "s", LEVELS)
    for path in series.levels:  # without an imaged size wsidicom cannot build the pyramid
        ds = pydicom.dcmread(path)
        del ds.ImagedVolumeWidth, ds.ImagedVolumeHeight
        ds.save_as(path)
    with pytest.raises(SlideMetadataError, match=r"could not be opened with the wsidicom") as err:
        open_dicom(tmp_path / "s")
    assert "level0" not in str(err.value)
    series = write_series(tmp_path / "t", LEVELS)
    src = open_dicom(tmp_path / "t")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError(f"cannot read {series.levels[0]}")

    monkeypatch.setattr(src._wsi, "read_region", broken)
    with pytest.raises(SlideReadError, match=r"Slide 's1' could not be decoded at level 0") as err:
        src.read_level(0, 0, 0, 8, 8)
    assert str(tmp_path) not in str(err.value)


def test_unexpected_failure_after_open_closes_the_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_series(tmp_path / "s", LEVELS)
    closed: list[bool] = []
    real_open = wsidicom.WsiDicom.open

    def opened(*args: Any, **kwargs: Any) -> Any:
        wsi = real_open(*args, **kwargs)
        real_close = wsi.close
        wsi.close = lambda: (closed.append(True), real_close())
        return wsi

    monkeypatch.setattr(wsidicom.WsiDicom, "open", opened)
    monkeypatch.setattr(dicom_reader, "_check_pyramid", _raise(ValueError("boom")))
    with pytest.raises(SlideMetadataError, match=r"wsidicom backend \(ValueError\)"):
        open_dicom(tmp_path / "s")
    monkeypatch.setattr(dicom_reader, "_check_pyramid", _raise(SlideMetadataError("refused")))
    with pytest.raises(SlideMetadataError, match="refused"):
        open_dicom(tmp_path / "s")
    assert closed == [True, True]


def _raise(err: Exception) -> Any:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise err

    return fail


def _level(level: int, w: int, h: int, sy: float = 1.0, planes: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        level=level,
        size=SimpleNamespace(width=w, height=h),
        pixel_spacing=SimpleNamespace(width=0.001 * 2**level, height=0.001 * 2**level * sy),
        focal_planes=[0.0] * planes,
        optical_paths=["1"],
    )


def test_pyramid_checks() -> None:
    good = [_level(0, 1000, 800), _level(2, 250, 200)]
    wsi = SimpleNamespace(pyramids=[good], levels=good)
    assert dicom_reader._check_pyramid(wsi, 2, "s1") == good
    with pytest.raises(SlideMetadataError, match="2 image pyramids"):
        dicom_reader._check_pyramid(SimpleNamespace(pyramids=[good, good], levels=good), 2, "s1")
    with pytest.raises(SlideMetadataError, match="3 level files that form 2 pyramid levels"):
        dicom_reader._check_pyramid(wsi, 3, "s1")
    bad: list[list[SimpleNamespace]] = [
        [_level(0, 1000, 800), _level(2, 250, 200, planes=3)],  # z-stack
        [_level(0, 1000, 800), _level(2, 250, 200, sy=2.0)],  # anisotropic
        [_level(0, 1000, 800), _level(2, 200, 200)],  # covers less than the slide
    ]
    for levels in bad:
        with pytest.raises(SlideMetadataError, match=r"focal planes|2\^k reduction"):
            dicom_reader._check_pyramid(SimpleNamespace(pyramids=[levels], levels=levels), 2, "s1")


def test_identity_never_leaves_the_headers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    write_series(tmp_path / "s", LEVELS)
    write_series(tmp_path / "rgb", LEVELS, codec="jpeg-rgb")
    src = open_dicom(tmp_path / "s")
    src.read_level(0, 0, 0, 300, 200)
    messages = [json.dumps(src.provenance), src.backend]
    with pytest.raises(SlideMetadataError) as err:
        open_dicom(tmp_path / "rgb")
    messages += [str(err.value), err.value.hint or ""] + [r.getMessage() for r in caplog.records]
    for secret in (PATIENT_NAME, PATIENT_ID, BARCODE):
        assert not any(secret in m for m in messages)
    assert str(read_header(src.fileset[0] / src.fileset[1][0]).PatientName) == PATIENT_NAME


def test_open_source_and_options(tmp_path: Path) -> None:
    write_series(tmp_path / "s", LEVELS)
    assert sniff(tmp_path / "s") == MATCH
    assert isinstance(open_source(tmp_path / "s", MATCH, name="s1"), DicomSource)
    with pytest.raises(SlideMetadataError, match="has no scene to choose"):
        open_reader(
            tmp_path / "s", MATCH, name="s1", mpp_override=None, options=ReaderOptions(scene=0)
        )


def test_registry_hashes_the_volume_files_only(tmp_path: Path) -> None:
    folder = tmp_path / "slides" / "dcm"
    series = write_series(folder, LEVELS)
    names = sorted(p.name for p in series.levels)
    digest = fileset_sha256(folder, names, file_sha256)
    series.others[0].write_bytes(b"a label changed after the hash was pinned")
    entries = [
        SlideEntry(id="d1", path="dcm"),
        SlideEntry(id="d2", path="dcm", fileset_sha256=digest),
        SlideEntry(id="d3", path="dcm", fileset_sha256="0" * 64),
    ]
    registry = SlideRegistry(entries, tmp_path / "slides")
    assert registry.get("d1").sha256 == digest
    assert registry.get("d2").sha256 == digest
    with pytest.raises(SlideIntegrityError, match="fileset_sha256"):
        registry.get("d3")
    registry.close()


# --- real slides ------------------------------------------------------------------------------


@pytest.mark.testdata
def test_leica_4_matches_openslide() -> None:
    folder = sample_path("DICOM/Leica-4", tier=2)
    src = open_dicom(folder)
    assert src.level_dimensions == ((23374, 22079), (5843, 5519), (1460, 1379))
    assert src.mpp == pytest.approx((1.05105387047, 1.05105387047), abs=1e-12)
    assert len(src.fileset[1]) == 3  # the label, overview and thumbnail files are left out
    region = src.read_level(0, 15744, 10144, 512, 512)
    assert region.reshape(-1, 3).mean(0) == pytest.approx([216.74, 207.83, 223.65], abs=0.01)
    import openslide

    base = next(p for p in sorted(folder.iterdir()) if p.name in src.fileset[1])
    with openslide.OpenSlide(base) as slide:
        ref = np.asarray(slide.read_region((15744, 10144), 0, (512, 512)))[..., :3]
        np.testing.assert_array_equal(region, ref)
        for level in (1, 2):  # OpenSlide reads lower levels exactly only at their origin
            w, h = 512, 512
            ref = np.asarray(slide.read_region((0, 0), level, (w, h)))[..., :3]
            np.testing.assert_array_equal(src.read_level(level, 0, 0, w, h), ref)
    single = open_dicom(base)
    assert single.fileset == src.fileset
    single.close()
    src.close()


@pytest.mark.testdata
def test_jp2k_ybr_full_422_colours() -> None:
    folder = sample_path("DICOM/JP2K-33003-1-v2", tier=3)
    src = open_dicom(folder)
    assert src.level_dimensions == ((15374, 17497), (3843, 4374), (1921, 2187))
    assert src.mpp == pytest.approx((0.2498, 0.2498))
    region = src.read_level(0, 7616, 12248, 512, 512)
    assert region.reshape(-1, 3).mean(0) == pytest.approx([202.02, 121.77, 156.55], abs=0.01)
    glass = src.read_level(2, 0, 0, 512, 512).reshape(-1, 3).mean(0)
    assert glass == pytest.approx([241.8, 241.3, 240.2], abs=0.1)  # a double colour transform
    # would give about (255, 123, 255)
    src.close()
