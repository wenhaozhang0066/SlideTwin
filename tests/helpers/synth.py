"""Synthetic slides with known geometry.

Pixels are white with 1-px black lines at known level-0 columns and rows. Pyramid levels are exact
block means of level 0, so every level covers the whole slide (the model used by the renderer).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import tifffile

from slidetwin.core.slide_source import RGB, ArraySlideSource

LINES_X = (300, 1000, 1501)
LINES_Y = (200, 777, 1200)


def line_image(width: int, height: int, xs: Sequence[int], ys: Sequence[int]) -> RGB:
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    for x in xs:
        img[:, x] = 0
    for y in ys:
        img[y, :] = 0
    return img


def pyramid(level0: RGB, n_levels: int) -> list[RGB]:
    levels = [level0]
    for k in range(1, n_levels):
        f = 2**k
        h, w = level0.shape[0] // f, level0.shape[1] // f
        block = level0[: h * f, : w * f].reshape(h, f, w, f, 3).astype(np.float64).mean(axis=(1, 3))
        levels.append(np.rint(block).astype(np.uint8))
    return levels


def line_source(
    width: int = 2048,
    height: int = 1536,
    mpp: float = 1.0,
    n_levels: int = 4,
    xs: Sequence[int] = LINES_X,
    ys: Sequence[int] = LINES_Y,
    name: str = "synthetic",
) -> ArraySlideSource:
    return ArraySlideSource(
        pyramid(line_image(width, height, xs, ys), n_levels), (mpp, mpp), name=name
    )


def write_pyramid_tiff(path: Path, levels: Sequence[RGB], mpp: float, *, tile: int = 256) -> Path:
    """Tiled pyramid with one top-level page per level (readable by tiffslide and OpenSlide)."""
    w0 = levels[0].shape[1]
    with tifffile.TiffWriter(path) as tif:
        for i, lv in enumerate(levels):
            f = w0 / lv.shape[1]
            res = 1e4 / (mpp * f)  # pixels per centimetre
            tif.write(
                lv,
                tile=(tile, tile),
                photometric="rgb",
                subfiletype=0 if i == 0 else 1,
                resolution=(res, res),
                resolutionunit="CENTIMETER",
                metadata=None,
            )
    return path


def dark_centroid(values: np.ndarray) -> float:
    """Sub-pixel position (pixel index units, centres at k) of a dark feature in a 1-D profile."""
    w = 255.0 - values.astype(np.float64)
    w[w < 8] = 0.0  # ignore ringing and quantisation noise
    return float((np.arange(len(w)) * w).sum() / w.sum())
