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


def _draw(rings: list[NDArray[np.float64]], size: tuple[int, int]) -> NDArray[np.bool_]:
    """Fill polygons given in grid units (cell k spans [k, k+1)) on a (cols, rows) raster."""
    img = Image.new("1", size, 0)
    draw = ImageDraw.Draw(img)
    for ring in rings:
        # PIL addresses a pixel by its index, so a vertex moves to the cell centre convention.
        draw.polygon([(float(x), float(y)) for x, y in (ring - 0.5).tolist()], fill=1, outline=1)
    return np.asarray(img, dtype=bool)


def build_lesions(regions: list[Region], grid_um: float = 8.0) -> Lesions:
    """Rasterise the regions on a grid (tumour minus exclusions) and merge touching tumour polygons
    (8-connected on this grid) into deposits. Draft definition: needs pathologist review."""
    tumor = [r for r in regions if r.group == "tumor"]
    if not tumor:
        return Lesions(grid_um, (0.0, 0.0), np.zeros((0, 0), bool), ())
    lo = np.min([r.ring.min(axis=0) for r in regions], axis=0) - grid_um
    hi = np.max([r.ring.max(axis=0) for r in regions], axis=0) + grid_um
    cols, rows = (int(v) for v in np.ceil((hi - lo) / grid_um))
    grid = [(r.ring - lo) / grid_um for r in tumor]
    tumor_mask = _draw(grid, (cols, rows))
    exclusions = [(r.ring - lo) / grid_um for r in regions if r.group == "exclusion"]
    mask = tumor_mask & ~_draw(exclusions, (cols, rows)) if exclusions else tumor_mask
    labels, _ = ndimage.label(tumor_mask, structure=np.ones((3, 3), bool))

    members: dict[int, list[Region]] = {}
    for region, ring in zip(tumor, grid, strict=True):
        c0, r0 = (max(0, int(v) - 1) for v in ring.min(axis=0))
        c1, r1 = (int(v) + 2 for v in ring.max(axis=0))
        alone = _draw([ring - (c0, r0)], (c1 - c0, r1 - r0))
        found = labels[r0:r1, c0:c1][alone[: rows - r0, : cols - c0]]
        found = found[found > 0]
        if found.size:
            members.setdefault(int(np.argmax(np.bincount(found))), []).append(region)

    deposits = []
    for k, rs in members.items():
        verts = np.concatenate([r.ring for r in rs])
        area = float((mask & (labels == k)).sum()) * grid_um * grid_um / 1e6
        (x0, y0), (x1, y1) = verts.min(axis=0), verts.max(axis=0)
        deposits.append(
            Deposit(
                round(area, 6),
                round(feret_diameter(verts) / 1000, 6),
                BBox(float(x0), float(y0), float(x1), float(y1)),
            )
        )
    deposits.sort(key=lambda d: (-d.largest_dimension_mm, -d.area_mm2))
    return Lesions(grid_um, (float(lo[0]), float(lo[1])), mask, tuple(deposits))
