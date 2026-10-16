"""Physical geometry: micrometres, magnification presets, pyramid levels and viewports.

Slide frame: micrometres, origin at the level-0 top-left corner, x right, y down.
All functions are pure.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from slidetwin.core.errors import InvalidParameter

Point = tuple[float, float]
Size = tuple[int, int]

# Relative slack for float comparisons of resolutions (absorbs rounding noise only).
EPS_REL = 1e-9


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


@dataclass(frozen=True, slots=True)
class BBox:
    """Axis-aligned rectangle in micrometres, half-open: [x0, x1) x [y0, y1)."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if not _finite(self.x0, self.y0, self.x1, self.y1):
            raise InvalidParameter(f"Rectangle coordinates must be finite, got {self.to_list()}.")
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise InvalidParameter(
                f"Rectangle has negative size: {self.to_list()}.",
                hint="Use [x0, y0, x1, y1] with x1 >= x0 and y1 >= y0.",
            )

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Point:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def to_list(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    def contains(self, x: float, y: float) -> bool:
        """Closed containment (edges included)."""
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def intersection(self, other: BBox) -> BBox | None:
        """Overlap with positive area, or None."""
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return BBox(x0, y0, x1, y1)


# --- magnification -------------------------------------------------------------------------


def format_mag(mag: float) -> str:
    return f"{mag:g}x"


def _check_ref(mpp_ref_40x: float) -> None:
    if not _finite(mpp_ref_40x) or mpp_ref_40x <= 0:
        raise InvalidParameter(f"mpp_ref_40x must be positive, got {mpp_ref_40x}.")


def mpp_for_mag(mag: float, mpp_ref_40x: float = 0.25) -> float:
    """Objective-equivalent convention: mpp(m) = mpp_ref_40x * 40 / m (um per pixel)."""
    if not _finite(mag) or mag <= 0:
        raise InvalidParameter(f"Magnification must be a positive number, got {mag}.")
    _check_ref(mpp_ref_40x)
    return mpp_ref_40x * 40.0 / mag


def mag_for_mpp(mpp: float, mpp_ref_40x: float = 0.25) -> float:
    """Inverse of `mpp_for_mag`: the objective-equivalent magnification of a resolution."""
    if not _finite(mpp) or mpp <= 0:
        raise InvalidParameter(f"Resolution must be a positive number of um/px, got {mpp}.")
    _check_ref(mpp_ref_40x)
    return mpp_ref_40x * 40.0 / mpp


def is_mag_available(
    mag: float, native_mpp: float, *, mpp_ref_40x: float = 0.25, tolerance: float = 0.06
) -> bool:
    """A preset is offered only if it is not finer than the scan (within `tolerance`)."""
    if not _finite(native_mpp) or native_mpp <= 0:
        raise InvalidParameter(f"Native resolution must be positive, got {native_mpp} um/px.")
    if not _finite(tolerance) or not 0 <= tolerance < 1:
        raise InvalidParameter(f"Tolerance must be in [0, 1), got {tolerance}.")
    return mpp_for_mag(mag, mpp_ref_40x) >= native_mpp * (1 - tolerance)


def available_mags(
    presets: Sequence[float],
    native_mpp: float,
    *,
    mpp_ref_40x: float = 0.25,
    tolerance: float = 0.06,
) -> tuple[float, ...]:
    return tuple(
        sorted(
            float(m)
            for m in presets
            if is_mag_available(m, native_mpp, mpp_ref_40x=mpp_ref_40x, tolerance=tolerance)
        )
    )


# --- pyramid levels ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LevelTransform:
    """Places a level's pixel grid on level 0, per axis: level-0 px = offset + scale * level px.

    Most pyramids span the whole slide, so the scale is the level-0 extent over the level extent
    and the offset is 0 (D13). A reader that knows the exact scale (e.g. 2^k when a level covers
    a few pixels less than the slide) gives its own.
    """

    scale_x: float
    scale_y: float
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        values = (self.scale_x, self.scale_y, self.offset_x, self.offset_y)
        if not _finite(*values) or self.scale_x <= 0 or self.scale_y <= 0:
            raise InvalidParameter(f"Invalid level transform {values}.")


def extent_transforms(level0_dims: Size, level_dims: Sequence[Size]) -> tuple[LevelTransform, ...]:
    """The default transform of every level: each level spans the whole slide."""
    w0, h0 = level0_dims
    return tuple(LevelTransform(w0 / w, h0 / h) for (w, h) in level_dims)


def transform_mpps(transforms: Sequence[LevelTransform], mpp_xy: Point) -> tuple[Point, ...]:
    """Per-axis um/px of every level."""
    mx, my = mpp_xy
    return tuple((mx * t.scale_x, my * t.scale_y) for t in transforms)


def level_mpps(level0_dims: Size, level_dims: Sequence[Size], mpp_xy: Point) -> tuple[Point, ...]:
    """Per-axis um/px of every level. Each level spans the whole slide, so its downsample on an
    axis is the level-0 extent divided by the level extent."""
    return transform_mpps(extent_transforms(level0_dims, level_dims), mpp_xy)


def choose_level(levels: Sequence[Point], target: Point) -> int:
    """Index of the coarsest level whose resolution is at least as fine as `target` on both axes.

    Falls back to the finest level when none qualifies (only possible inside the magnification
    tolerance, where a few percent of upsampling is accepted).
    """
    if not levels:
        raise InvalidParameter("A slide needs at least one pyramid level.")
    tx, ty = target
    best: int | None = None
    best_ratio = -1.0
    for i, (lx, ly) in enumerate(levels):
        ratio = max(lx / tx, ly / ty)  # >1 means coarser than the target
        if ratio <= 1 + EPS_REL and ratio > best_ratio:
            best, best_ratio = i, ratio
    if best is not None:
        return best
    return min(range(len(levels)), key=lambda i: max(levels[i][0] / tx, levels[i][1] / ty))


# --- viewports ------------------------------------------------------------------------------


def fov_um(viewport_px: Size, mpp: float) -> tuple[float, float]:
    """Physical field of view of a viewport."""
    w, h = viewport_px
    if w <= 0 or h <= 0:
        raise InvalidParameter(f"Viewport must have a positive size, got {w}x{h} px.")
    if not _finite(mpp) or mpp <= 0:
        raise InvalidParameter(f"Resolution must be positive, got {mpp} um/px.")
    return (w * mpp, h * mpp)


def viewport_bbox(center: Point, viewport_px: Size, mpp: float) -> BBox:
    fw, fh = fov_um(viewport_px, mpp)
    cx, cy = center
    return BBox(cx - fw / 2, cy - fh / 2, cx + fw / 2, cy + fh / 2)


def clamp_center(center: Point, fov: tuple[float, float], slide_um: tuple[float, float]) -> Point:
    """Keep the field of view inside the slide.

    On an axis where the field of view is wider than the slide, centre the view on that axis.
    """
    out = []
    for c, f, s in zip(center, fov, slide_um, strict=True):
        if f >= s:
            out.append(s / 2)
        else:
            out.append(min(max(c, f / 2), s - f / 2))
    return (out[0], out[1])


def view_px_to_um(p_px: Point, bbox: BBox, mpp: float) -> Point:
    return (bbox.x0 + p_px[0] * mpp, bbox.y0 + p_px[1] * mpp)


def um_to_view_px(p_um: Point, bbox: BBox, mpp: float) -> Point:
    return ((p_um[0] - bbox.x0) / mpp, (p_um[1] - bbox.y0) / mpp)


def distance(p: Point, q: Point) -> float:
    return math.hypot(q[0] - p[0], q[1] - p[1])


def displacement_in_viewports(c0: Point, c1: Point, fov: tuple[float, float]) -> float:
    """Length of a move in viewport units (x in widths, y in heights)."""
    return math.hypot((c1[0] - c0[0]) / fov[0], (c1[1] - c0[1]) / fov[1])


# --- thumbnails and crops ---------------------------------------------------------------------


def thumbnail_size(slide_um: tuple[float, float], long_side_px: int, native_mpp: float) -> Size:
    """Thumbnail of the whole slide: long side at most `long_side_px`, never finer than the scan."""
    w, h = slide_um
    if not _finite(w, h) or w <= 0 or h <= 0:
        raise InvalidParameter(f"Slide size must be positive, got {w} x {h} um.")
    mpp = max(max(w, h) / long_side_px, native_mpp)
    size = []
    for extent in (w, h):
        n = max(1, round(extent / mpp))
        if n > 1 and extent / n < native_mpp:  # rounding up would upsample
            n -= 1
        size.append(n)
    return (size[0], size[1])


def crop_geometry(
    requested: BBox, slide_um: tuple[float, float], long_side_px: int, native_mpp: float
) -> tuple[BBox, Size, float]:
    """Resolve a free crop: clamp to the slide, scale the long side to `long_side_px` but never
    finer than the scan, and keep pixels square.

    Returns (actual bbox, output size, output mpp). The actual bbox keeps the clamped top-left
    corner; its size is the output size times the output mpp (within half a pixel of the request).
    """
    if requested.width <= 0 or requested.height <= 0:
        raise InvalidParameter(
            "Crop width and height must be positive, "
            f"got {requested.width} x {requested.height} um."
        )
    clipped = requested.intersection(BBox(0.0, 0.0, slide_um[0], slide_um[1]))
    if clipped is None:
        raise InvalidParameter(
            f"Crop rectangle {requested.to_list()} lies outside the slide "
            f"(0..{slide_um[0]:.1f} x 0..{slide_um[1]:.1f} um).",
        )
    mpp = max(max(clipped.width, clipped.height) / long_side_px, native_mpp)
    size = (max(1, round(clipped.width / mpp)), max(1, round(clipped.height / mpp)))
    bbox = BBox(clipped.x0, clipped.y0, clipped.x0 + size[0] * mpp, clipped.y0 + size[1] * mpp)
    return bbox, size, mpp
