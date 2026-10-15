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


PINK, PURPLE = (232, 140, 190), (120, 60, 160)


def tissue_image(
    width: int, height: int, rects: Sequence[tuple[int, int, int, int, tuple[int, int, int]]]
) -> RGB:
    """White glass with coloured rectangles (x, y, w, h, rgb) standing in for H&E tissue."""
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    for x, y, w, h, rgb in rects:
        img[y : y + h, x : x + w] = rgb
    return img


def tissue_source(
    width: int = 2048,
    height: int = 1536,
    mpp: float = 1.0,
    rects: Sequence[tuple[int, int, int, int, tuple[int, int, int]]] = (
        (100, 100, 800, 600, PINK),  # piece 1: 0.48 mm2 at 1 um/px
        (1200, 900, 400, 400, PURPLE),  # piece 2: 0.16 mm2
    ),
    n_levels: int = 4,
    name: str = "tissue",
) -> ArraySlideSource:
    return ArraySlideSource(
        pyramid(tissue_image(width, height, rects), n_levels), (mpp, mpp), name=name
    )


TISSUE_PIECES_PX = ((300, 300, 1800, 1500, PINK), (2500, 1900, 800, 700, PURPLE))


def write_tissue_slide(
    directory: Path,
    *,
    size_px: tuple[int, int] = (4096, 3072),
    mpp: float = 0.5,
    pieces: Sequence[tuple[int, int, int, int, tuple[int, int, int]]] = TISSUE_PIECES_PX,
    slide_id: str = "syn_tissue",
    eval_only: dict[str, str] | None = None,
) -> Path:
    """A pyramid TIFF with tissue-coloured rectangles and a manifest naming it; returns the
    manifest path. Default: native 20x, 2048 x 1536 um, two tissue pieces."""
    import yaml  # tests only

    write_pyramid_tiff(
        directory / f"{slide_id}.tif", pyramid(tissue_image(*size_px, pieces), 5), mpp
    )
    entry: dict[str, object] = {"id": slide_id, "path": f"{slide_id}.tif", "license": "CC0-1.0"}
    if eval_only:
        entry["eval_only"] = eval_only
    manifest = directory / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({"slides": [entry]}))
    return manifest
