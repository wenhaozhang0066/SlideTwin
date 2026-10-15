"""Lesion annotations for evaluation only (SPEC 13): ASAP XML in, micrometre GeoJSON out.

Nothing here is reachable from a session: the core, the REST and MCP adapters and the agents never
import `slidetwin.eval` (a test checks this). Files go to the git-ignored `data/annotations/`.

Groups. CAMELYON16 uses "Tumor" (or "_0" / "_1") for metastases and "Exclusion" (or "_2") for
normal tissue inside them. Coordinates in the XML are level-0 pixels; they become micrometres with
the slide's own mpp, the same frame the sessions use.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw
from scipy import ndimage

from slidetwin.core.geometry import BBox

TUMOR_GROUPS = {"tumor", "_0", "_1", "metastases"}
EXCLUSION_GROUPS = {"exclusion", "_2", "none-tumor", "normal"}

Ring = NDArray[np.float64]  # (n, 2) vertices in um, not closed


@dataclass(frozen=True)
class Region:
    group: str  # "tumor" or "exclusion"
    ring: Ring
    name: str = ""

    @property
    def bbox(self) -> BBox:
        (x0, y0), (x1, y1) = self.ring.min(axis=0), self.ring.max(axis=0)
        return BBox(float(x0), float(y0), float(x1), float(y1))


def _group(raw: str) -> str | None:
    key = raw.strip().lower()
    if key in TUMOR_GROUPS:
        return "tumor"
    if key in EXCLUSION_GROUPS:
        return "exclusion"
    return None


def parse_asap(xml: str | bytes, mpp: tuple[float, float]) -> list[Region]:
    """Polygons of an ASAP annotation file, in um. Unknown groups and degenerate polygons (fewer
    than three vertices) are skipped."""
    root = ET.fromstring(xml)
    regions = []
    for ann in root.iter("Annotation"):
        group = _group(ann.get("PartOfGroup", ""))
        if group is None or ann.get("Type", "Polygon") not in {"Polygon", "Spline", "Rectangle"}:
            continue
        coords = sorted(ann.iter("Coordinate"), key=lambda c: int(float(c.get("Order", "0"))))
        pts = np.array([[float(c.get("X", "nan")), float(c.get("Y", "nan"))] for c in coords])
        if len(pts) < 3 or not np.isfinite(pts).all():
            continue
        regions.append(Region(group, pts * np.asarray(mpp), ann.get("Name", "")))
    return regions


def to_geojson(regions: list[Region], *, slide_id: str, source: str) -> dict[str, Any]:
    features = [
        {
            "type": "Feature",
            "properties": {"group": r.group, "name": r.name},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[*np.round(r.ring, 3).tolist(), np.round(r.ring[0], 3).tolist()]],
            },
        }
        for r in regions
    ]
    return {
        "type": "FeatureCollection",
        "properties": {"slide_id": slide_id, "source": source, "units": "um"},
        "features": features,
    }


def from_geojson(data: dict[str, Any]) -> list[Region]:
    regions = []
    for f in data["features"]:
        ring = np.asarray(f["geometry"]["coordinates"][0], dtype=np.float64)
        if len(ring) > 1 and np.array_equal(ring[0], ring[-1]):
            ring = ring[:-1]
        regions.append(Region(f["properties"]["group"], ring, f["properties"].get("name", "")))
    return regions


def load_regions(path: Path) -> list[Region]:
    return from_geojson(json.loads(path.read_text(encoding="utf-8")))


# --- lesions -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Deposit:
    """A contiguous tumour deposit: annotated polygons whose rasters touch, merged."""

    area_mm2: float
    largest_dimension_mm: float  # greatest distance between two of its points (Feret diameter)
    bbox_um: BBox


@dataclass(frozen=True)
class Lesions:
    grid_um: float
    origin_um: tuple[float, float]
    mask: NDArray[np.bool_]  # tumour minus exclusions, on the grid
    deposits: tuple[Deposit, ...]  # largest first

    @property
    def largest_mm(self) -> float | None:
        return self.deposits[0].largest_dimension_mm if self.deposits else None

    def slices(self, bbox: BBox) -> tuple[slice, slice]:
        """Rows and columns of the grid cells whose centre lies inside `bbox`."""
        g = self.grid_um
        rows, cols = self.mask.shape
        x0, y0 = self.origin_um

        def span(a: float, b: float, origin: float, n: int) -> slice:
            lo = min(n, max(0, math.ceil((a - origin) / g - 0.5)))
            hi = min(n, max(0, math.ceil((b - origin) / g - 0.5)))
            return slice(lo, max(lo, hi))

        return span(bbox.y0, bbox.y1, y0, rows), span(bbox.x0, bbox.x1, x0, cols)

    def cells(self, bbox: BBox) -> NDArray[np.bool_]:
        """The tumour cells whose centre lies inside `bbox`."""
        rs, cs = self.slices(bbox)
        out: NDArray[np.bool_] = self.mask[rs, cs]
        return out

    def hits(self, bbox: BBox) -> bool:
        return bool(self.cells(bbox).any())

    @property
    def area_cells(self) -> int:
        return int(self.mask.sum())


def convex_hull(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Andrew's monotone chain; returns the hull vertices counter-clockwise."""
    pts = np.unique(points, axis=0)
    if len(pts) <= 2:
        return pts

    def half(seq: NDArray[np.float64]) -> list[NDArray[np.float64]]:
        out: list[NDArray[np.float64]] = []
        for p in seq:
            while len(out) >= 2:
                a, b = out[-2], out[-1]
                if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    lower, upper = half(pts), half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def feret_diameter(points: NDArray[np.float64]) -> float:
    """The greatest distance between two of the points (over their convex hull)."""
    hull = convex_hull(points)
    if len(hull) < 2:
        return 0.0
    best = 0.0
    for i in range(0, len(hull), 512):  # chunked: hulls of large deposits have many vertices
        d = hull[i : i + 512, None, :] - hull[None, :, :]
        best = max(best, float(np.sqrt((d**2).sum(axis=2)).max()))
    return best


SUPERSAMPLE = 8


def _fill(ring: NDArray[np.float64], shape: tuple[int, int]) -> tuple[int, int, NDArray[np.bool_]]:
    """Cells of a raster of `shape` (rows, cols) whose centre lies inside the polygon `ring`
    (grid units: cell k spans [k, k+1)). Returns (row0, col0, block) for the polygon's box.

    The polygon is drawn 8x finer and sampled at the cell centres, so Pillow's rounding of the
    vertices moves an edge by at most 1/8 of a cell, the same way on every side.
    """
    rows, cols = shape
    c0, r0 = (max(0, int(np.floor(v))) for v in ring.min(axis=0))
    c1, r1 = (int(np.ceil(v)) for v in ring.max(axis=0))
    c1, r1 = min(cols, max(c1, c0 + 1)), min(rows, max(r1, r0 + 1))
    if c0 >= cols or r0 >= rows:
        return r0, c0, np.zeros((0, 0), dtype=bool)
    s = SUPERSAMPLE
    img = Image.new("1", ((c1 - c0) * s, (r1 - r0) * s), 0)
    pts = (ring - (c0, r0)) * s
    ImageDraw.Draw(img).polygon([(float(x), float(y)) for x, y in pts.tolist()], fill=1)
    block: NDArray[np.bool_] = np.asarray(img, dtype=bool)[s // 2 :: s, s // 2 :: s]
    return r0, c0, block


def _raster(rings: list[NDArray[np.float64]], shape: tuple[int, int]) -> NDArray[np.bool_]:
    mask = np.zeros(shape, dtype=bool)
    for ring in rings:
        r0, c0, block = _fill(ring, shape)
        mask[r0 : r0 + block.shape[0], c0 : c0 + block.shape[1]] |= block
    return mask


def _shoelace_um2(ring: Ring) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def _overlap(a: BBox, b: BBox) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def build_lesions(regions: list[Region], grid_um: float = 8.0) -> Lesions:
    """Rasterise the regions on a grid (a cell is tumour when its centre lies in a tumour polygon
    and in no exclusion) and group 8-connected tumour cells into deposits. Draft definition:
    needs pathologist review.

    A deposit's size is the largest distance between two vertices of its polygons. Where an
    exclusion lies on a deposit, the polygons no longer describe its outline, so the size comes
    from the grid instead: the largest distance between two of its cell centres, plus one cell.
    A polygon smaller than one cell keeps its own area and size.
    """
    tumor = [r for r in regions if r.group == "tumor"]
    if not tumor:
        return Lesions(grid_um, (0.0, 0.0), np.zeros((0, 0), bool), ())
    lo = np.min([r.ring.min(axis=0) for r in regions], axis=0) - grid_um
    hi = np.max([r.ring.max(axis=0) for r in regions], axis=0) + grid_um
    cols, rows = (int(v) for v in np.ceil((hi - lo) / grid_um))
    shape = (rows, cols)

    def to_grid(ring: Ring) -> NDArray[np.float64]:
        grid: NDArray[np.float64] = (ring - lo) / grid_um
        return grid

    exclusions = [r for r in regions if r.group == "exclusion"]
    mask = _raster([to_grid(r.ring) for r in tumor], shape)
    if exclusions:
        mask &= ~_raster([to_grid(r.ring) for r in exclusions], shape)
    labels, _ = ndimage.label(mask, structure=np.ones((3, 3), bool))

    members: dict[int, list[Region]] = {}
    split: set[int] = set()  # components holding part of a polygon that an exclusion cut
    deposits = []
    for region in tumor:
        r0, c0, block = _fill(to_grid(region.ring), shape)
        found = labels[r0 : r0 + block.shape[0], c0 : c0 + block.shape[1]][block]
        found = np.unique(found[found > 0])
        if found.size:
            for k in found:
                members.setdefault(int(k), []).append(region)
            if found.size > 1:
                split.update(int(k) for k in found)
        elif not block.any():  # smaller than a cell: a deposit of its own
            deposits.append(
                Deposit(
                    round(_shoelace_um2(region.ring) / 1e6, 6),
                    round(feret_diameter(region.ring) / 1000, 6),
                    region.bbox,
                )
            )

    for k, rs in members.items():
        cells = np.argwhere(labels == k)  # (row, col)
        centres = (cells[:, ::-1] + 0.5) * grid_um + lo
        (x0, y0), (x1, y1) = centres.min(axis=0) - grid_um / 2, centres.max(axis=0) + grid_um / 2
        box = BBox(float(x0), float(y0), float(x1), float(y1))
        near = BBox(box.x0 - grid_um, box.y0 - grid_um, box.x1 + grid_um, box.y1 + grid_um)
        if k in split or any(_overlap(near, e.bbox) for e in exclusions):
            size_um = feret_diameter(centres) + grid_um
        else:
            verts = np.concatenate([r.ring for r in rs])
            size_um = feret_diameter(verts)
            (x0, y0), (x1, y1) = verts.min(axis=0), verts.max(axis=0)
            box = BBox(float(x0), float(y0), float(x1), float(y1))
        area = float(len(cells)) * grid_um * grid_um / 1e6
        deposits.append(Deposit(round(area, 6), round(size_um / 1000, 6), box))
    deposits.sort(key=lambda d: (-d.largest_dimension_mm, -d.area_mm2))
    return Lesions(grid_um, (float(lo[0]), float(lo[1])), mask, tuple(deposits))
