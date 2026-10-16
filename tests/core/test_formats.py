"""Format router (M8): every signature, and refusals that name the slide id but no file."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError, SlideNotFound
from slidetwin.core.formats import MAX_TILES, FormatMatch, check_tiff_size, sniff

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


def ifd_tiff(
    entries: list[tuple[int, int, int, int]], *, big: bool = False, next_ifd: int = 0
) -> bytes:
    """A minimal little-endian TIFF with one IFD of (tag, type, count, value) entries."""
    if big:
        head = b"II+\x00" + struct.pack("<HHQ", 8, 0, 16)
        body = struct.pack("<Q", len(entries))
        body += b"".join(struct.pack("<HHQQ", *e) for e in entries)
        return head + body + struct.pack("<Q", next_ifd)
    head = b"II*\x00" + struct.pack("<I", 8)
    body = struct.pack("<H", len(entries)) + b"".join(struct.pack("<HHII", *e) for e in entries)
    return head + body + struct.pack("<I", next_ifd)


@pytest.mark.parametrize("big", [False, True])
def test_tiffs_declaring_huge_arrays_are_refused_before_parsing(tmp_path: Path, big: bool) -> None:
    tiles = ifd_tiff([(256, 4, 1, 1024), (257, 4, 1, 1024), (324, 4, MAX_TILES + 1, 4096)], big=big)
    pixels = ifd_tiff([(256, 4, 1, 1 << 21), (257, 4, 1, 1 << 20), (324, 4, 1, 4096)], big=big)
    for data in (tiles, pixels):
        path = write(tmp_path / "x.tif", data)
        with pytest.raises(SlideMetadataError, match="implausibly many"):
            check_tiff_size(path, "s1")
        with pytest.raises(SlideMetadataError, match="implausibly many"):
            sniff(path, "s1")  # before tifffile loads the arrays
    short = ifd_tiff([(256, 3, 1, 512), (257, 3, 1, 512), (324, 4, 64, 4096)], big=big)
    check_tiff_size(write(tmp_path / "ok.tif", short), "s1")  # plausible: tifffile decides


def test_unusual_ifds_are_left_to_tifffile(tmp_path: Path) -> None:
    looped = ifd_tiff([(256, 4, 1, 64)], next_ifd=8)  # the next IFD is itself
    empty = ifd_tiff([])
    truncated = ifd_tiff([(256, 4, 1, 64), (257, 4, 1, 64)])[:20]
    no_count = b"II*\x00" + struct.pack("<I", 4096)  # the IFD lies past the end
    no_next = ifd_tiff([(256, 4, 1, 64)])[:-2]  # the next-IFD pointer is cut short
    for i, data in enumerate((looped, empty, truncated, no_count, no_next)):
        check_tiff_size(write(tmp_path / f"u{i}.tif", data), "s1")  # returns, never loops
