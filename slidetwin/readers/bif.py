"""Ventana BIF slides from the VENTANA DP 200 scanner (M8): a stitcher over tifffile tiles.

The pyramid IFDs are described 'level=N mag=M ...'; the label and probability images are never
read. Each level-0 tile is one camera frame. The XMP of the level-0 IFD records, for every pair of
neighbouring frames, how far they overlap (TileJointInfo). Frames are placed column by column with
those overlaps, rows abut, and lower levels hold the same frames downsampled: a frame lands at
ceil(x / 2^k), later frames on top, as Bio-Formats does. This was bit-identical to Bio-Formats on
Ventana-1 at all 8 levels. Level k is floor(width / 2^k) wide at a scale of exactly 2^k (the extent
ratio is off by up to 0.04% there). mpp comes from iScan ScanRes.

Only the case validated on Ventana-1 is served: scanner VENTANA DP 200, one scanned area, LEFT and
UP joints, horizontal overlaps that are the same in every row, no vertical ones, every joint
confirmed (confidence 98 or more, as Bio-Formats requires), one focal plane. Older iScan files
(RIGHT joints, averaged overlaps) and anything else are refused.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.tiffpages import (
    TiffPagesSource,
    checked_mpp,
    is_rgb8,
    layout_refusal,
    level_array,
    open_with,
    provenance,
    refusal,
)

FORMAT = "ventana-bif"
SCANNER = "VENTANA DP 200"
STITCHER = "xmp joints per column, frames at ceil(x / 2^k), later frame wins"
MIN_CONFIDENCE = 98  # Bio-Formats ignores joints below this
_LEVEL = re.compile(r"level=(\d+) mag=[\d.]+")


@dataclass(frozen=True)
class Frames:
    """Frame placement at one level: left edges of the frame columns, the frame width, and where
    the scanned area starts in the stored level (first column, first row in pixels)."""

    left: NDArray[np.int64]
    width: int
    first_col: int
    first_row: int


class BifSource(TiffPagesSource):
    def __init__(self, *args: Any, frames: list[Frames], **kw: Any) -> None:
        self._frames = frames
        super().__init__(*args, **kw)

    def _read(self, level: int, x: int, y: int, w: int, h: int) -> NDArray[Any]:
        f = self._frames[level]
        pos = np.arange(x, x + w, dtype=np.int64)
        col = np.searchsorted(f.left, pos, side="right") - 1  # the last frame that starts here
        stored = (f.first_col + col) * f.width + pos - f.left[col]
        x0, y0 = int(stored[0]), f.first_row + y
        block = np.asarray(self._arrays[level][y0 : y0 + h, x0 : int(stored[-1]) + 1])
        return block[:, stored - x0]


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> BifSource:
    options.refuse_unused(name, FORMAT)
    return open_with(path, name, lambda tif: _build(tif, name, mpp_override))


def _pyramid(tif: Any, name: str) -> list[Any]:
    """The 'level=N' IFDs in level order: tiled 8-bit RGB, one plane, each exactly 2^k smaller."""
    found: dict[int, list[Any]] = {}
    for page in tif.pages:
        described = _LEVEL.match(page.description or "")
        if described:
            found.setdefault(int(described.group(1)), []).append(page)
    ok = sorted(found) == list(range(len(found))) and all(len(p) == 1 for p in found.values())
    pages = [found[k][0] for k in sorted(found)] if ok else []
    for k, page in enumerate(pages):
        ok = ok and page.is_tiled and page.imagedepth == 1 and is_rgb8(page)
        ok = ok and (page.imagewidth << k, page.imagelength << k) == (
            pages[0].imagewidth,
            pages[0].imagelength,
        )
    if not (ok and pages):
        raise layout_refusal(name, FORMAT)
    return pages


def _unsupported(name: str, what: str) -> SlideMetadataError:
    return refusal(
        name,
        FORMAT,
        f"is {what}; SlideTwin reads single-area DP 200 BIF files only.",
        hint="Export the slide to OME-TIFF or DICOM with the vendor's software.",
    )


def _column_overlaps(info: Any, rows: int, cols: int, width: int, name: str) -> list[int]:
    """How far each frame column slides over the one to its left, from the joints. Tiles are
    numbered from 1 in a serpentine starting at the bottom row (as OpenSlide and Bio-Formats)."""

    def cell(number: int) -> tuple[int, int]:
        q, k = divmod(number - 1, cols)
        return rows - 1 - q, k if q % 2 == 0 else cols - 1 - k

    found: dict[int, set[int]] = {}
    ok = True
    for joint in info.findall("TileJointInfo"):
        direction = joint.get("Direction")
        if direction not in ("LEFT", "UP"):
            raise _unsupported(name, f"an iScan file with {direction} joints")
        (r1, c1), (r2, c2) = cell(int(joint.get("Tile1", 0))), cell(int(joint.get("Tile2", 0)))
        dx, dy = float(joint.get("OverlapX", "nan")), float(joint.get("OverlapY", "nan"))
        confirmed = int(joint.get("Confidence", 0)) >= MIN_CONFIDENCE
        ok = joint.get("FlagJoined") == "1" and confirmed and dy == 0
        ok = ok and all(0 <= r < rows and 0 <= c < cols for r, c in ((r1, c1), (r2, c2)))
        if direction == "LEFT":
            ok = ok and r1 == r2 and abs(c1 - c2) == 1 and dx.is_integer() and 0 <= dx < width
        else:
            ok = ok and c1 == c2 and abs(r1 - r2) == 1 and dx == 0
        if not ok:
            break
        if direction == "LEFT":
            found.setdefault(min(c1, c2), set()).add(int(dx))
    if not ok or any(len(values) != 1 for values in found.values()):
        raise _unsupported(name, "a scan whose frame joints SlideTwin has not validated")
    return [next(iter(found.get(b, {0}))) for b in range(cols - 1)]


def _build(tif: Any, name: str, override: Point | None) -> BifSource:
    pages = _pyramid(tif, name)
    xmp = pages[0].tags.valueof(700)
    text = xmp.rstrip(b"\x00 \r\n\t") if isinstance(xmp, bytes) else b""  # it may end in NUL
    if not text:
        raise layout_refusal(name, FORMAT)  # no stitching information
    root = ElementTree.fromstring(text)
    scan = root.find("SlideInfo/iScan")
    if scan is None or scan.get("ScannerModel") != SCANNER:
        raise _unsupported(name, "not from the VENTANA DP 200 scanner")
    if scan.get("Z-layers", "1") != "1":
        raise _unsupported(name, "a z-stack")
    infos = root.findall("SlideStitchInfo/ImageInfo")
    if len(infos) != 1 or infos[0].get("AOIScanned") != "1":
        raise _unsupported(name, "not a scan of exactly one area")
    info = infos[0]
    rows, cols = int(info.get("NumRows", 0)), int(info.get("NumCols", 0))
    frame = (int(info.get("Width", 0)), int(info.get("Height", 0)))
    origin = root.find(f"AoiOrigin/AOI{info.get('AOIIndex', '0')}")
    first = (-1, -1)
    if origin is not None:
        first = (int(origin.get("OriginX", -1)), int(origin.get("OriginY", -1)))
    page0 = pages[0]
    ends = (first[0] + cols * frame[0], first[1] + rows * frame[1])
    ok = rows > 0 and cols > 0 and frame == (page0.tilewidth, page0.tilelength)
    ok = ok and min(first) >= 0 and first[0] % frame[0] == 0 and first[1] % frame[1] == 0
    ok = ok and ends[0] <= page0.imagewidth and ends[1] <= page0.imagelength
    ok = ok and all(frame[0] % 2**k == 0 and frame[1] % 2**k == 0 for k in range(len(pages)))
    if not ok:
        raise layout_refusal(name, FORMAT)
    shift = _column_overlaps(info, rows, cols, frame[0], name)
    xs = [c * frame[0] - sum(shift[:c]) for c in range(cols)]
    w0, h0 = xs[-1] + frame[0], rows * frame[1]
    frames = []
    for k in range(len(pages)):
        ds = 2**k
        left = np.array([math.ceil(x / ds) for x in xs], dtype=np.int64)
        frames.append(Frames(left, frame[0] // ds, first[0] // frame[0], first[1] // ds))
    try:
        res = float(scan.get("ScanRes", "nan"))
    except ValueError:
        res = math.nan
    mpp, source = checked_mpp((res, res), "bif iScan ScanRes", override, name)
    return BifSource(
        tif,
        [level_array(page) for page in pages],
        [(w0 >> k, h0 >> k) for k in range(len(pages))],
        mpp,
        name,
        provenance=provenance(FORMAT, source, stitcher=STITCHER),
        transforms=[LevelTransform(2.0**k, 2.0**k) for k in range(len(pages))],
        frames=frames,
    )
