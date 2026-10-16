"""Trestle TIFF slides (M8): a stitcher over tifffile tiles.

All pages of the TIFF are levels; the sidecar files next to it are never read. The first levels
store overlapping camera tiles (OverlapsXY in the description, per level). They are laid on a fixed
grid with step tile - overlap, and the later (right or lower) tile wins each overlap, as OpenSlide
and Bio-Formats do; this was bit-identical to OpenSlide on CMU-1. These levels are exactly 2^k.
The coarser levels have no overlap and cover level 0 less half the level-0 overlap on each side,
so a level-k pixel X starts at level-0 pixel overlap / 2 + 2^k X (on CMU-1: (X0 - 32) / 2^k,
verified at 24 points per level). XResolution and YResolution hold um/px.

Only the structure verified on CMU-1 is served: whole tiles on the overlapped levels, levels exactly
2^k apart, coarse levels exactly the size above, and um/px tags that double per level. Anything
else is refused.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point, Size
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.tiffpages import (
    TiffPagesSource,
    checked_mpp,
    is_rgb8,
    layout_refusal,
    level_array,
    open_with,
    provenance,
)

FORMAT = "trestle-tiff"
STITCHER = "grid of tile - overlap, later tile wins; coarse levels at half the overlap"


@dataclass(frozen=True)
class Grid:
    """An overlapped level: tile size, step (tile - overlap) and tiles across and down."""

    tile: Size
    step: Size
    count: Size


class TrestleSource(TiffPagesSource):
    def __init__(self, *args: Any, grids: list[Grid | None], **kw: Any) -> None:
        self._grids = grids
        super().__init__(*args, **kw)

    def _read(self, level: int, x: int, y: int, w: int, h: int) -> NDArray[Any]:
        grid = self._grids[level]
        if grid is None:
            return super()._read(level, x, y, w, h)
        cols = _stored(x, w, grid.tile[0], grid.step[0], grid.count[0])
        rows = _stored(y, h, grid.tile[1], grid.step[1], grid.count[1])
        x0, y0 = int(cols[0]), int(rows[0])
        block = np.asarray(self._arrays[level][y0 : int(rows[-1]) + 1, x0 : int(cols[-1]) + 1])
        return block[np.ix_(rows - y0, cols - x0)]


def _stored(start: int, size: int, tile: int, step: int, count: int) -> NDArray[np.int64]:
    """Stored coordinate of each stitched coordinate. Tile c owns [c * step, (c + 1) * step); the
    last tile owns the rest of its width."""
    pos = np.arange(start, start + size, dtype=np.int64)
    owner = np.minimum(pos // step, count - 1)
    stored: NDArray[np.int64] = owner * tile + pos - owner * step
    return stored


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> TrestleSource:
    options.refuse_unused(name, FORMAT)
    return open_with(path, name, lambda tif: _build(tif, name, mpp_override))


def overlaps(description: str) -> list[Size]:
    """The (x, y) overlap of each overlapped level, from 'OverlapsXY= 64 64 32 32 16 16'."""
    for item in description.split(";"):
        key, _, value = item.partition("=")
        if key.strip() == "OverlapsXY":
            numbers = [int(v) for v in value.split()]
            if len(numbers) % 2 == 0:
                return list(zip(numbers[::2], numbers[1::2], strict=True))
    return []


def _um_per_px(page: Any) -> tuple[float, float] | None:
    xres, yres = page.tags.valueof("XResolution"), page.tags.valueof("YResolution")
    if not xres or not yres or not xres[1] or not yres[1]:
        return None
    return (xres[0] / xres[1], yres[0] / yres[1])


def _levels(pages: list[Any], ov: list[Size], name: str) -> tuple[list[Grid | None], list[Size]]:
    """Each level's grid (None for coarse levels) and its size as the slide shows it."""
    if not (0 < len(ov) <= len(pages) and all(x > 0 and y > 0 for x, y in ov)):
        raise layout_refusal(name, FORMAT)
    if not all(page.is_tiled and is_rgb8(page) for page in pages):
        raise layout_refusal(name, FORMAT)
    grids: list[Grid | None] = []
    dims: list[Size] = []
    ok = True
    for k, page in enumerate(pages):
        size = (page.imagewidth, page.imagelength)
        if k < len(ov):  # overlapped: whole tiles on a grid of tile - overlap
            tile = (page.tilewidth, page.tilelength)
            count = (size[0] // tile[0], size[1] // tile[1])
            step = (tile[0] - ov[k][0], tile[1] - ov[k][1])
            ok = ok and size == (count[0] * tile[0], count[1] * tile[1]) and min(step) > 0
            grids.append(Grid(tile, step, count))
            dims.append((count[0] * step[0] + ov[k][0], count[1] * step[1] + ov[k][1]))
            ok = ok and (dims[k][0] << k, dims[k][1] << k) == dims[0]
        else:  # coarse: the level-0 image less half the overlap on each side, 2^k smaller
            grids.append(None)
            dims.append(size)
            inner = (dims[0][0] - ov[0][0], dims[0][1] - ov[0][1])
            ok = ok and (size[0] << k, size[1] << k) == inner
    if not ok:
        raise layout_refusal(name, FORMAT)
    return grids, dims


def _build(tif: Any, name: str, override: Point | None) -> TrestleSource:
    pages = list(tif.pages)
    ov = overlaps(pages[0].description)
    grids, dims = _levels(pages, ov, name)
    raw = _um_per_px(pages[0])
    for k, page in enumerate(pages):
        level = _um_per_px(page)
        if (
            raw is None
            or level is None
            or not all(
                math.isclose(a, b * 2**k, rel_tol=1e-6) for a, b in zip(level, raw, strict=True)
            )
        ):
            raw = None  # not um/px that double per level: this reading is not confirmed
    mpp, source = checked_mpp(raw, "trestle XResolution as um/px", override, name)
    half = (ov[0][0] / 2, ov[0][1] / 2)
    transforms = [
        LevelTransform(2.0**k, 2.0**k, *((0.0, 0.0) if grid else half))
        for k, grid in enumerate(grids)
    ]
    return TrestleSource(
        tif,
        [level_array(page) for page in pages],
        dims,
        mpp,
        name,
        provenance=provenance(FORMAT, source, stitcher=STITCHER),
        transforms=transforms,
        grids=grids,
    )
