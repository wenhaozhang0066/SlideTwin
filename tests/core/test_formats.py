"""Format router (M8): every signature, and refusals that name the slide id but no file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError, SlideNotFound
from slidetwin.core.formats import FormatMatch, sniff

RGB = np.full((64, 64, 3), 200, dtype=np.uint8)


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def tiff(path: Path, **kw: Any) -> Path:
    extratags = kw.pop("extratags", ())
    with tifffile.TiffWriter(path) as tif:
        tif.write(RGB, tile=(16, 16), photometric="rgb", metadata=None, extratags=extratags, **kw)
    return path


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("a.dcm", b"\x00" * 128 + b"DICM" + b"\x00" * 64, ("dicom", "wsidicom")),
        ("a.czi", b"ZISRAWFILE" + b"\x00" * 64, ("zeiss-czi", "czi")),
        ("a.zvi", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, ("ole2", "zvi")),
        ("a.svslide", b"SQLite format 3\x00" + b"\x00" * 64, ("sakura-svslide", "openslide")),
        ("a.mrxs", b"\xff\xd8\xff\xe0 preview", ("mirax", "openslide")),
        ("a.vms", b"[Virtual Microscope Specimen]\nNoLayers=1\n", ("hamamatsu-vms", "openslide")),
        (
            "b.vmu",
            b"\xef\xbb\xbf[Uncompressed Virtual Microscope Specimen]\n",
            ("hamamatsu-vms", "openslide"),
        ),
        (
            "a.isyntax",
            b'<?xml version="1.0"?><DataObject ObjectType="DPUfsImport">',
            ("philips-isyntax", "isyntax"),
        ),
        ("a.vsi", b"II*\x00" + b"\x00" * 64, ("olympus-vsi", "slideio")),
        ("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64, ("plain-image", None)),
        ("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, ("plain-image", None)),
        ("a.bmp", b"BM" + b"\x00" * 64, ("plain-image", None)),
        ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", ("plain-image", None)),
        ("a.jp2", b"\x00\x00\x00\x0cjP  \r\n\x87\n" + b"\x00" * 64, ("plain-image", None)),
        ("a.j2k", b"\xff\x4f\xff\x51" + b"\x00" * 64, ("plain-image", None)),
        ("a.sdpc", b"SQ1.1.9.0430" + b"\x00" * 64, ("sqray-sdpc", None)),
        ("a.tmap", b"TMAP07" + b"\x00" * 64, ("unic-tmap", None)),
        ("a.mdsx", b"BKIO" + b"\x00" * 64, ("motic-mds", None)),
        ("a.dyj", b"DPTWSI01" + b"\x00" * 64, ("dyj", None)),
        ("a.bin", b"something else entirely", ("unknown", None)),
    ],
)
def test_signatures(tmp_path: Path, name: str, data: bytes, expected: tuple[str, Any]) -> None:
    match = sniff(write(tmp_path / name, data))
    assert (match.format, match.reader) == expected
    assert (match.reader is None) == bool(match.reason)  # a refusal always says what to do


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, "generic-tiff"),
        ({"description": "Aperio Image Library v12 \n2220x2967 | MPP = 0.499"}, "aperio-svs"),
        ({"description": "Motic 1.0 |MPP = 0.25"}, "motic-tiff"),
        ({"description": "Mikroscan Image Structure v1"}, "mikroscan-tiff"),
        ({"description": '<?xml version="1.0"?><scn xmlns="x"></scn>'}, "leica-scn"),
        ({"description": '<?xml version="1.0"?><OME xmlns="x"></OME>'}, "ome-tiff"),
        (
            {
                "software": "Philips DP v1.0",
                "description": '<DataObject ObjectType="DPUfsImport"></DataObject>',
            },
            "philips-tiff",
        ),
        ({"software": "MedScan", "description": "Trestle Corp."}, "trestle-tiff"),
        (
            {"software": "PerkinElmer-QPI", "description": "<PerkinElmer-QPI-ImageDescription/>"},
            "akoya-qptiff",
        ),
        ({"software": "Ventana", "extratags": [(700, "B", 4, b"<x/>", True)]}, "ventana-bif"),
        (
            {"extratags": [(271, "s", 0, "Hamamatsu", True), (65420, "I", 1, 1, True)]},
            "hamamatsu-ndpi",
        ),
        ({"extratags": [(271, "s", 0, "Huron Digital", True)]}, "huron-tiff"),
        ({"extratags": [(65000, "s", 0, "<Argos version='1'/>", True)]}, "argos-avs"),
    ],
)
def test_tiff_vendors(tmp_path: Path, kw: dict[str, Any], expected: str) -> None:
    assert sniff(tiff(tmp_path / "s.tif", **kw)).format == expected


def test_folders(tmp_path: Path) -> None:
    zarr_dir = tmp_path / "image.zarr"
    zarr_dir.mkdir()
    (zarr_dir / "zarr.json").write_text("{}")
    assert sniff(zarr_dir) == FormatMatch("ome-zarr", "zarr")
    series = tmp_path / "series"
    series.mkdir()
    write(series / "a.txt", b"notes")
    write(series / "b", b"\x00" * 128 + b"DICM")
    assert sniff(series).format == "dicom"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert sniff(empty).reader is None


def test_missing_and_damaged_files_name_only_the_slide(tmp_path: Path) -> None:
    with pytest.raises(SlideNotFound, match="slide 'pub_1'") as err:
        sniff(tmp_path / "secret_label_name.tif", "pub_1")
    assert "secret" not in err.value.message
    broken = write(tmp_path / "secret.tif", b"II*\x00" + b"\xff" * 32)
    with pytest.raises(SlideMetadataError, match="Slide 'pub_1' is not a readable TIFF") as err2:
        sniff(broken, "pub_1")
    assert "secret" not in err2.value.message
