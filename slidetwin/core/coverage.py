"""Tissue mask, tissue pieces and coverage (SPEC 5.4 E4, SPEC 13).

Everything lives on the coverage grid: square cells of `grid_um` micrometres, origin at the level-0
top-left corner. The mask comes from the slide pixels only (a saturation threshold), never from
annotations, and is a pure function of the slide and the protocol's coverage settings.
"""

from __future__ import annotations

import math
import threading
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import ndimage

from slidetwin.core.config import CoverageRules
from slidetwin.core.geometry import BBox
from slidetwin.core.render import render_region
from slidetwin.core.slide_source import RGB, SlideSource, slide_size_um

METHOD = "saturation-v2"
_EIGHT = np.ones((3, 3), dtype=bool)


@dataclass(frozen=True)
class Piece:
    piece: int  # 1 is the largest piece
    bbox_um: BBox
    area_mm2: float

    def to_dict(self) -> dict[str, Any]:
        return {"piece": self.piece, "bbox_um": self.bbox_um.to_list(), "area_mm2": self.area_mm2}


@dataclass(frozen=True)
class TissueMap:
    grid_um: float
    mask: NDArray[np.bool_]  # (rows, cols)
    labels: NDArray[np.int32]  # 0 background, k = piece k
    pieces: tuple[Piece, ...]
    size_um: tuple[float, float] = (math.inf, math.inf)  # slide extent; it cuts the edge cells

    @property
    def shape(self) -> tuple[int, int]:
        rows, cols = self.mask.shape
        return int(rows), int(cols)

    def _centres(self, n: int, extent: float) -> NDArray[np.float64]:
        """Centres of the parts of n cells that lie inside [0, extent]."""
        lo = np.arange(n, dtype=np.float64) * self.grid_um
        centres: NDArray[np.float64] = (lo + np.minimum(lo + self.grid_um, extent)) / 2
        return centres

    def cells_in(self, bbox: BBox) -> tuple[slice, slice]:
        """Cells whose centre lies inside `bbox` (half-open).

        A cell cut by the right or bottom slide border is centred on its part inside the slide:
        views are clamped to the slide, so the full-cell centre of an edge cell could lie outside
        every possible view.
        """
        rows, cols = self.shape
        xs = self._centres(cols, self.size_um[0])
        ys = self._centres(rows, self.size_um[1])
        c0, c1 = (int(np.searchsorted(xs, v, side="left")) for v in (bbox.x0, bbox.x1))
        r0, r1 = (int(np.searchsorted(ys, v, side="left")) for v in (bbox.y0, bbox.y1))
        return slice(r0, max(r0, r1)), slice(c0, max(c0, c1))


def otsu_threshold(values: NDArray[np.uint8]) -> int:
    """Otsu's threshold on 8-bit values: the t maximising between-class variance of (<= t, > t)."""
    hist = np.bincount(values.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0
    levels = np.arange(256, dtype=np.float64)
    w0 = np.cumsum(hist)
    m0 = np.cumsum(hist * levels)
    w1 = total - w0
    mean_all = m0[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mean_all * w0 / total - m0) ** 2 / (w0 * w1 / total)
    between[~np.isfinite(between)] = -1.0
    return int(np.argmax(between))


def saturation(rgb: RGB) -> NDArray[np.uint8]:
    """HSV saturation on a 0-255 scale."""
    arr = rgb.astype(np.int32)
    hi = arr.max(axis=2)
    lo = arr.min(axis=2)
    sat = np.zeros(hi.shape, dtype=np.int32)
    np.divide((hi - lo) * 255, hi, out=sat, where=hi > 0, casting="unsafe")
    return sat.astype(np.uint8)


def tissue_mask(rgb: RGB, *, min_saturation: int, min_cells: int) -> NDArray[np.bool_]:
    """Tissue where the (3x3 median-filtered) saturation exceeds `min_saturation`.

    Closing and hole filling turn fat lobules and lumina into tissue; pieces smaller than
    `min_cells` are dropped. Saturation ignores grey and black artifacts (slide borders, dust).
    """
    sat = ndimage.median_filter(saturation(rgb), size=3, mode="nearest")
    mask = sat > min_saturation
    pad = 2  # closing near the image edge must not erode tissue that touches it
    padded = np.pad(mask, pad, mode="edge")
    closed = ndimage.binary_closing(padded, structure=_EIGHT, iterations=pad)[pad:-pad, pad:-pad]
    mask = ndimage.binary_fill_holes(closed)
    if min_cells > 1 and mask.any():
        labels, n = ndimage.label(mask, structure=_EIGHT)
        sizes = np.bincount(labels.ravel(), minlength=n + 1)
        keep = sizes >= min_cells
        keep[0] = False
        mask = keep[labels]
    out: NDArray[np.bool_] = mask
    return out


def build_tissue_map(source: SlideSource, rules: CoverageRules) -> TissueMap:
    g = rules.grid_um
    w_um, h_um = slide_size_um(source)
    cols, rows = max(1, math.ceil(w_um / g)), max(1, math.ceil(h_um / g))
    rgb = render_region(source, BBox(0.0, 0.0, cols * g, rows * g), (cols, rows))
    min_cells = max(1, math.ceil(rules.tissue_min_area_um2 / (g * g)))
    mask = tissue_mask(rgb, min_saturation=rules.tissue_min_saturation, min_cells=min_cells)
    raw, n = ndimage.label(mask, structure=_EIGHT)
    sizes = np.bincount(raw.ravel(), minlength=n + 1)[1:]
    boxes = ndimage.find_objects(raw)
    # number pieces by descending area; ties broken by position (top, then left)
    order = sorted(range(n), key=lambda i: (-sizes[i], boxes[i][0].start, boxes[i][1].start))
    relabel = np.zeros(n + 1, dtype=np.int32)
    pieces = []
    for rank, i in enumerate(order, start=1):
        relabel[i + 1] = rank
        rs, cs = boxes[i]
        bbox = BBox(cs.start * g, rs.start * g, min(cs.stop * g, w_um), min(rs.stop * g, h_um))
        pieces.append(Piece(rank, bbox, round(float(sizes[i]) * g * g / 1e6, 4)))
    labels = relabel[raw].astype(np.int32)
    return TissueMap(g, mask, labels, tuple(pieces), (w_um, h_um))


_CACHE: weakref.WeakKeyDictionary[Any, dict[str, TissueMap]] = weakref.WeakKeyDictionary()
_CACHE_LOCK = threading.Lock()


def tissue_map(source: SlideSource, rules: CoverageRules) -> TissueMap:
    """Shared, deterministic tissue map of an open slide for given coverage settings.

    Cached per slide source (the registry keeps one source per slide, so every session on a slide
    shares it); an entry disappears with its source.
    """
    key = f"{METHOD}|{rules.grid_um}|{rules.tissue_min_saturation}|{rules.tissue_min_area_um2}"
    with _CACHE_LOCK:
        cached = _CACHE.get(source, {}).get(key)
    if cached is not None:
        return cached
    built = build_tissue_map(source, rules)
    with _CACHE_LOCK:
        return _CACHE.setdefault(source, {}).setdefault(key, built)


@dataclass(frozen=True)
class Coverage:
    fraction: float  # covered tissue / tissue in scope (1.0 when the scope holds no tissue)
    tissue_mm2: float
    covered_mm2: float
    min_mag: float
    grid_um: float
    seen: NDArray[np.bool_]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fraction": round(self.fraction, 6),
            "tissue_mm2": round(self.tissue_mm2, 4),
            "covered_mm2": round(self.covered_mm2, 4),
            "at_or_above_mag": self.min_mag,
            "grid_um": self.grid_um,
        }


def seen_cells(tissue: TissueMap, bboxes: Iterable[BBox]) -> NDArray[np.bool_]:
    seen = np.zeros(tissue.shape, dtype=bool)
    for bbox in bboxes:
        rs, cs = tissue.cells_in(bbox)
        seen[rs, cs] = True
    return seen


def coverage(
    tissue: TissueMap,
    seen: NDArray[np.bool_],
    min_mag: float,
    scope: NDArray[np.bool_] | None = None,
) -> Coverage:
    in_scope = tissue.mask if scope is None else tissue.mask & scope
    tissue_cells = int(in_scope.sum())
    covered_cells = int((in_scope & seen).sum())
    cell_mm2 = tissue.grid_um * tissue.grid_um / 1e6
    fraction = covered_cells / tissue_cells if tissue_cells else 1.0
    return Coverage(
        fraction, tissue_cells * cell_mm2, covered_cells * cell_mm2, min_mag, tissue.grid_um, seen
    )
