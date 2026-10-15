"""Pixel heuristics the baseline agents run on images the environment served them.

They are deliberately simple (SPEC 12: the baselines demonstrate coverage proofs and rule checks,
not diagnostic accuracy). Nothing here reads the slide or the server's tissue map.
"""

from __future__ import annotations

import io

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage

RGB = NDArray[np.uint8]
Mask = NDArray[np.bool_]


def decode(jpeg: bytes) -> RGB:
    with Image.open(io.BytesIO(jpeg)) as img:
        return np.asarray(img.convert("RGB"))


def saturation(rgb: RGB) -> NDArray[np.float64]:
    """HSV saturation on 0-255."""
    x = rgb.astype(np.float64)
    hi = x.max(axis=2)
    lo = x.min(axis=2)
    return np.where(hi > 0, (hi - lo) / np.maximum(hi, 1) * 255.0, 0.0)


def tissue_mask(rgb: RGB, min_saturation: float = 15.0) -> Mask:
    """Stained tissue: saturated pixels after a 3x3 median filter (glass is nearly grey)."""
    mask: Mask = ndimage.median_filter(saturation(rgb), size=3) > min_saturation
    return mask


def dense_purple(rgb: RGB) -> Mask:
    """Dark, bluish pixels: haematoxylin-dense nuclei or cell clusters."""
    x = rgb.astype(np.int16)
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mask: Mask = (r < 160) & (g < 120) & (b > r - 10) & (b - g > 40)
    return mask


def purple_fraction(rgb: RGB) -> float:
    """Share of the tissue pixels in `rgb` that are dense purple (0 when there is no tissue)."""
    tissue = tissue_mask(rgb)
    n = int(tissue.sum())
    return float((dense_purple(rgb) & tissue).sum() / n) if n else 0.0


def mask_box(mask: Mask) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of the largest connected region of `mask`, in pixels."""
    labels, n = ndimage.label(mask)
    if n == 0:
        return None
    sizes = ndimage.sum_labels(mask, labels, index=np.arange(1, n + 1))
    ys, xs = np.nonzero(labels == int(np.argmax(sizes)) + 1)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
