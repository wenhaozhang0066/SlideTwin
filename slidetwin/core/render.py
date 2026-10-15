"""Rendering backend: turns a physical rectangle into pixels (the `ViewerBackend` seam, SPEC 6.3).

Environment semantics (rules, timing, logging) live in `session.py`; this module only knows how to
produce pixels from a `SlideSource`. A future backend for another viewer implements the same
function signatures.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

from slidetwin.core.errors import InvalidParameter, SlideMetadataError
from slidetwin.core.geometry import BBox, Size, choose_level
from slidetwin.core.slide_source import RGB, SlideSource, source_level_mpps

# Lanczos-3 support is 3 output pixels; one extra pixel of margin.
_LANCZOS_SUPPORT = 3
# Refuse reads that would need more than this many source pixels (pyramid far too shallow).
MAX_READ_PIXELS = 400_000_000


def render_region(
    source: SlideSource,
    bbox: BBox,
    out_size: Size,
    *,
    fill_rgb: tuple[int, int, int] = (255, 255, 255),
) -> RGB:
    """Render the micrometre rectangle `bbox` into an RGB image of `out_size` (w, h).

    1. Pick the coarsest level that is at least as fine as the output on both axes.
    2. If that level is still >= 2x finer (missing pyramid levels), reduce it by an integer box
       filter first.
    3. Resample the exact fractional source box with Lanczos (sub-pixel placement). Pixels outside
       the slide are `fill_rgb`.
    The result is a pure function of the inputs.
    """
    out_w, out_h = out_size
    if out_w <= 0 or out_h <= 0:
        raise InvalidParameter(f"Output size must be positive, got {out_w}x{out_h} px.")
    if bbox.width <= 0 or bbox.height <= 0:
        raise InvalidParameter(f"Region must have a positive area, got {bbox.to_list()}.")
    tx, ty = bbox.width / out_w, bbox.height / out_h

    levels = source_level_mpps(source)
    level = choose_level(levels, (tx, ty))
    lx, ly = levels[level]
    lw, lh = source.level_dimensions[level]

    scale = min(tx / lx, ty / ly)
    k = math.floor(scale) if scale >= 2 else 1
    vx, vy = lx * k, ly * k  # pixel size of the (possibly reduced) source, um
    vw, vh = -(-lw // k), -(-lh // k)

    fx0, fy0, fx1, fy1 = bbox.x0 / vx, bbox.y0 / vy, bbox.x1 / vx, bbox.y1 / vy
    pad = math.ceil(_LANCZOS_SUPPORT * max(tx / vx, ty / vy)) + 1
    ix0, iy0 = math.floor(fx0) - pad, math.floor(fy0) - pad
    ix1, iy1 = math.ceil(fx1) + pad, math.ceil(fy1) + pad

    cx0, cy0 = max(ix0, 0), max(iy0, 0)
    cx1, cy1 = min(ix1, vw), min(iy1, vh)
    read_px = max(0, cx1 - cx0) * max(0, cy1 - cy0) * k * k
    if read_px > MAX_READ_PIXELS:
        raise SlideMetadataError(
            f"Rendering this region would read {read_px:,} pixels; "
            "the slide pyramid is too shallow.",
            hint="Use a slide with a multi-resolution pyramid.",
        )

    canvas = np.empty((iy1 - iy0, ix1 - ix0, 3), dtype=np.uint8)
    canvas[...] = fill_rgb
    if cx0 < cx1 and cy0 < cy1:
        x0, y0 = cx0 * k, cy0 * k
        x1, y1 = min(cx1 * k, lw), min(cy1 * k, lh)
        patch = source.read_level(level, x0, y0, x1 - x0, y1 - y0)
        if k > 1:
            patch = np.asarray(Image.fromarray(patch).reduce(k), dtype=np.uint8)
        oy, ox = cy0 - iy0, cx0 - ix0
        canvas[oy : oy + patch.shape[0], ox : ox + patch.shape[1]] = patch

    box = (fx0 - ix0, fy0 - iy0, fx1 - ix0, fy1 - iy0)
    out = Image.fromarray(canvas).resize((out_w, out_h), resample=Image.Resampling.LANCZOS, box=box)
    return np.asarray(out, dtype=np.uint8)
