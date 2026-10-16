"""Ventana BIF reader (M8): DP 200 frames stitched from the XMP joints, as Bio-Formats does."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.bif import BifSource, open_reader
from tests.conftest import sample_path
from tests.helpers.make_bif import SOFTWARE, write_bif

MATCH = FormatMatch("ventana-bif", "bif")


def open_bif(path: Path, **kw: Any) -> BifSource:
    kw.setdefault("mpp_override", None)
    options = kw.pop("options", ReaderOptions())
    return open_reader(path, MATCH, name="s1", options=options, **kw)


def full(src: BifSource, level: int) -> np.ndarray:
    w, h = src.level_dimensions[level]
    return src.read_level(level, 0, 0, w, h)


def refused(path: Path, match: str, **kw: Any) -> None:
    with pytest.raises(SlideMetadataError, match=match) as err:
        open_bif(path, **kw)
    assert "SECRET" not in f"{err.value.message} {err.value.hint}"


@pytest.mark.parametrize("origin", [(0, 0), (1, 2)])
def test_frames_are_stitched_like_bioformats(tmp_path: Path, origin: tuple[int, int]) -> None:
    f = write_bif(tmp_path / "a.bif", origin_cells=origin)
    assert sniff(f.path) == MATCH
    src = open_source(f.path, sniff(f.path), name="s1")
    assert isinstance(src, BifSource)
    # four 64-px frame columns with overlaps 0, 5, 3: 256 - 8 = 248 wide; rows abut
    assert src.level_dimensions == ((248, 192), (124, 96), (62, 48), (31, 24))
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(4))
    assert src.mpp == (0.25, 0.25)
    for k in range(4):  # frames at ceil(x / 2^k), later frames on top
        np.testing.assert_array_equal(full(src, k), f.stitched[k])
    rng = np.random.default_rng(1)
    for _ in range(20):
        x, y = int(rng.integers(0, 200)), int(rng.integers(0, 150))
        np.testing.assert_array_equal(
            src.read_level(0, x, y, 41, 33), f.stitched[0][y : y + 33, x : x + 41]
        )
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"]) == ("ventana-bif", "bif iScan ScanRes")
    assert prov["stitcher"] == "xmp joints per column, frames at ceil(x / 2^k), later frame wins"
    assert "SECRET" not in json.dumps(prov)
    src.close()


def test_label_and_probability_images_are_never_read(tmp_path: Path) -> None:
    f = write_bif(tmp_path / "a.bif")
    with tifffile.TiffFile(f.path, mode="r+b") as tif:  # their pixels now point past the file
        for page in tif.pages[:2]:
            page.tags["StripOffsets"].overwrite([2**40] * len(page.tags["StripOffsets"].value))
    src = open_bif(f.path)
    np.testing.assert_array_equal(full(src, 0), f.stitched[0])


def _scan(xmp: str) -> str:
    return xmp.replace('ScannerModel="VENTANA DP 200"', 'ScannerModel="iScan Coreo"')


def _two_areas(xmp: str) -> str:
    extra = (
        '<ImageInfo AOIScanned="0" AOIIndex="1" NumRows="1" NumCols="1" Width="64" Height="64"/>'
    )
    return xmp.replace("<FrameInfo", extra + "<FrameInfo")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda x: x.replace('Direction="LEFT"', 'Direction="RIGHT"'),
            "iScan file with RIGHT joints",
        ),
        (_scan, "not from the VENTANA DP 200 scanner"),
        (lambda x: x.replace('Z-layers="1"', 'Z-layers="3"'), "is a z-stack"),
        (_two_areas, "not a scan of exactly one area"),
        (
            lambda x: x.replace('<ImageInfo AOIScanned="1"', '<ImageInfo AOIScanned="0"'),
            "exactly one area",
        ),
        (lambda x: x.replace('OverlapX="5"', 'OverlapX="6"', 1), "frame joints"),  # differs by row
        (
            lambda x: x.replace('OverlapY="0"', 'OverlapY="2"', 1),
            "frame joints",
        ),  # a vertical overlap
        (lambda x: x.replace('Confidence="100"', 'Confidence="40"', 1), "frame joints"),
        (lambda x: x.replace('FlagJoined="1"', 'FlagJoined="0"', 1), "frame joints"),
        (lambda x: x.replace('OverlapX="5"', 'OverlapX="64"'), "frame joints"),  # a whole frame
        (lambda x: x.replace('Tile2="2"', 'Tile2="4"', 1), "frame joints"),  # not neighbours
        (lambda x: x.replace('Tile2="12"', 'Tile2="99"', 1), "frame joints"),  # no such tile
        (
            lambda x: x.replace('Direction="UP" Tile1="1"', 'Direction="UP" Tile1="2"'),
            "frame joints",
        ),
        (
            lambda x: x.replace(
                'OverlapX="0" OverlapY="0"/></ImageInfo>', 'OverlapX="1" OverlapY="0"/></ImageInfo>'
            ),
            "frame joints",
        ),  # UP with dx
    ],
)
def test_unvalidated_scans_are_refused(
    tmp_path: Path, edit: Callable[[str], str], message: str
) -> None:
    path = write_bif(tmp_path / "a.bif", edit_xmp=edit).path
    refused(path, message)


@pytest.mark.parametrize(
    "kw",
    [
        {"level_description": lambda k: f"level={k + (k > 1)} mag=10"},  # a level missing
        {"level_description": lambda k: f"level={min(k, 2)} mag=10"},  # one level twice
        {"levels": 8},  # 64-px frames cannot be halved seven times
        {"edit_xmp": lambda x: x.replace('Width="64"', 'Width="32"')},  # frames are not the tiles
        {"edit_xmp": lambda x: x.replace('OriginX="0"', 'OriginX="10"')},  # not on the tile grid
        {"edit_xmp": lambda x: x.replace('NumCols="4"', 'NumCols="9"')},  # wider than the TIFF
        {"edit_xmp": lambda x: x.replace("<AoiOrigin><AOI0", "<AoiOrigin><AOI7")},  # no origin
        {"empty_xmp": True},  # no stitching information at all
    ],
)
def test_other_layouts_are_refused(tmp_path: Path, kw: dict[str, Any]) -> None:
    if kw.get("empty_xmp"):
        path = write_bif(tmp_path / "a.bif").path
        with tifffile.TiffFile(path, mode="r+b") as tif:
            tif.pages[2].tags["XMP"].overwrite(b"")
    else:
        path = write_bif(tmp_path / "a.bif", **kw).path
    refused(path, "layout SlideTwin validated")


def test_level_sizes_must_halve_exactly_and_be_one_plane(tmp_path: Path) -> None:
    f = write_bif(tmp_path / "a.bif")
    odd = tmp_path / "odd.bif"
    with tifffile.TiffWriter(odd, bigtiff=True) as tw:
        tw.write(
            np.zeros((90, 40, 3), np.uint8),
            description="Label_Image",
            software=SOFTWARE,
            metadata=None,
            extratags=[(700, "B", 0, b"<x/>", True)],
        )
        with tifffile.TiffFile(f.path) as src:
            xmp = src.pages[2].tags["XMP"].value
        for k, size in enumerate([(192, 320), (96, 159)]):  # 320 px, then 159 instead of 160
            tw.write(
                np.zeros((*size, 3), np.uint8),
                tile=(64, 64),
                photometric="rgb",
                description=f"level={k} mag=40 quality=95",
                metadata=None,
                extratags=[(700, "B", 0, xmp, True)] if k == 0 else [],
            )
    refused(odd, "layout SlideTwin validated")
    deep = write_bif(tmp_path / "deep.bif", imagedepth=2).path
    refused(deep, "layout SlideTwin validated")


def test_scan_resolution_is_the_mpp(tmp_path: Path) -> None:
    path = write_bif(
        tmp_path / "a.bif", edit_xmp=lambda x: x.replace('ScanRes="0.25"', 'ScanRes="0.2325"')
    ).path
    assert open_bif(path).mpp == (0.2325, 0.2325)
    for res in ("abc", "0"):
        bad = write_bif(
            tmp_path / f"{res}.bif",
            edit_xmp=lambda x, r=res: x.replace('ScanRes="0.25"', f'ScanRes="{r}"'),
        ).path
        refused(bad, "refusing to guess")
        assert open_bif(bad, mpp_override=(0.25, 0.25)).provenance["mpp_source"] == "mpp_override"


def test_no_scene_plane_or_channels(tmp_path: Path) -> None:
    path = write_bif(tmp_path / "a.bif").path
    refused(path, "has no scene to choose", options=ReaderOptions(scene=0))


# --- the CC0 sample (tier 3, local) ----------------------------------------------------------

# Bio-Formats 8.5.0 and the survey's independent stitcher agree on these (sha256 of RGB rows).
GOLDEN_LEVELS = {
    3: "31254b81f75e6b99ebd88dc9e39477a4781188ae3a935dfb0caa553e1d8b44fb",
    4: "22b9667cc6326f907e233625c04a4e7250a8a2df26bb0063e123c7130556c33e",
    5: "d7ab1e729d8e218eda8dbc7b1dc5ab5cf843921e24195e8548b5d8496d7a532c",
    6: "8630b07129a74dbd1cfaad9aa2fd7b73a5f2090cf426fc288910078a0760b6a3",
    7: "ddc6d2d2852e332b91f63e4555c8a2f06f5de9375186ed3dced23879705f6f57",
}
GOLDEN_REGIONS = {  # level-0 512 x 512 windows
    (3328, 9472): "865e46e575022ad1a1045f52c3385a25512a8e25e226e1f25df8034902dc783e",
    (11460, 10496): "0322c44ba03193bfe95032138c324f5492829b470d6540876209fe411304d3e1",
}


def _sha(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


@pytest.mark.testdata
def test_ventana1_is_bit_identical_to_bioformats() -> None:
    path = sample_path("Ventana/Ventana-1.bif", tier=3)
    src = open_bif(path)
    assert src.level_dimensions == (
        (23432, 21504),
        (11716, 10752),
        (5858, 5376),
        (2929, 2688),
        (1464, 1344),
        (732, 672),
        (366, 336),
        (183, 168),
    )
    assert src.mpp == (0.25, 0.25)
    for level, digest in GOLDEN_LEVELS.items():
        assert _sha(full(src, level)) == digest, level
    for (x, y), digest in GOLDEN_REGIONS.items():
        assert _sha(src.read_level(0, x, y, 512, 512)) == digest
