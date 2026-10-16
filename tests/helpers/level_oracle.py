"""The block-mean oracle of the M8 review: where a level's pixels really lie on level 0.

Pixel X of a level whose transform has scale s and offset o covers level 0 from o + s X to
o + s (X + 1). If the transform is right, the s x s box means of level 0 there match the level's
pixels better than at any placement nearby. A box at a half-pixel position is the mean of the
whole-pixel boxes around it, so placements are compared in steps of half a level-0 pixel.
"""

from __future__ import annotations

import math

import numpy as np

from slidetwin.core.slide_source import SlideSource, source_level_transforms


def box_means(img: np.ndarray, s: int) -> np.ndarray:
    """The mean of every s x s window of an (h, w, channels) image, at whole-pixel positions."""
    c = np.cumsum(np.cumsum(img.astype(np.float64), 0), 1)
    c = np.pad(c, ((1, 0), (1, 0), (0, 0)))
    return (c[s:, s:] - c[:-s, s:] - c[s:, :-s] + c[:-s, :-s]) / (s * s)


def misplacement(
    src: SlideSource, level: int, x: int, y: int, size: int, reach: int = 6
) -> tuple[float, float]:
    """How far from where its transform puts it the window (x, y) of `level`, size x size pixels,
    matches the box means of level 0 best (least squares): level-0 pixels per axis, searched in
    half pixels up to `reach` half pixels each way. (0, 0) when the transform is right. The
    window's part of level 0, with a margin, must lie inside level 0."""
    t = source_level_transforms(src)[level]
    s = round(t.scale_x)
    left, top = t.offset_x + s * x, t.offset_y + s * y  # where the transform puts the window
    margin = reach + 2
    x0, y0 = math.floor(left) - margin, math.floor(top) - margin
    span = s * size + 2 * margin
    means = box_means(src.read_level(0, x0, y0, span, span), s)
    target = src.read_level(level, x, y, size, size).astype(np.float64)
    hx, hy = round(2 * (left - x0)), round(2 * (top - y0))  # in half pixels from (x0, y0)

    def placed(px: int, py: int) -> np.ndarray:
        """Box means of level 0 from (px / 2, py / 2), at the level's pixel pitch."""
        cols = [px // 2] if px % 2 == 0 else [px // 2, px // 2 + 1]
        rows = [py // 2] if py % 2 == 0 else [py // 2, py // 2 + 1]
        boxes = [means[r::s, c::s][:size, :size] for r in rows for c in cols]
        return np.asarray(sum(boxes)) / len(boxes)

    errors = {
        (dx, dy): float(((placed(hx + dx, hy + dy) - target) ** 2).mean())
        for dx in range(-reach, reach + 1)
        for dy in range(-reach, reach + 1)
    }
    dx, dy = min(errors, key=errors.__getitem__)
    return dx / 2, dy / 2


def tissue_windows(src: SlideSource, level: int, size: int, count: int) -> list[tuple[int, int]]:
    """Up to `count` windows of `level` (their corners, level pixels), size x size pixels, centred
    on the most stained pixels of the coarsest level and far enough from the slide's edges for
    `misplacement`."""
    coarse = len(src.level_dimensions) - 1
    cw, ch = src.level_dimensions[coarse]
    thumb = src.read_level(coarse, 0, 0, cw, ch).astype(int)
    stain = thumb.max(axis=2) - thumb.min(axis=2)
    ys, xs = np.nonzero(stain > max(20, np.percentile(stain, 90)))
    order = np.argsort(-stain[ys, xs], kind="stable")
    transforms = source_level_transforms(src)
    tc, tl = transforms[coarse], transforms[level]
    w0, h0 = src.level_dimensions[0]
    s, room = round(tl.scale_x), 2 * (size + 16)
    out: list[tuple[int, int]] = []
    for i in order[:: max(1, len(order) // (4 * count))]:
        cx = tc.offset_x + tc.scale_x * (xs[i] + 0.5)  # on level 0
        cy = tc.offset_y + tc.scale_y * (ys[i] + 0.5)
        x = int((cx - tl.offset_x) / s) - size // 2
        y = int((cy - tl.offset_y) / s) - size // 2
        inside = s * x > room and s * y > room
        if inside and s * (x + size) + room < w0 and s * (y + size) + room < h0:
            out.append((x, y))
        if len(out) == count:
            break
    return out
