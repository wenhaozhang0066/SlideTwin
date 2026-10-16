"""Small Trestle TIFF files with known stitched pixels (ported from the M8 survey's
make_synth_trestle_cr.py).

The stitched level 0 is a gradient (red follows x, green follows y) plus noise in blue. Overlapped
levels store tiles cut from the stitched image with step = tile - overlap; the part of each tile
that the next tile covers is painted green, so only "the later tile wins" stitches it back exactly.
Coarse levels have no overlap and cover level 0 without half the level-0 overlap on each side, as
measured on CMU-1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from slidetwin.core.slide_source import RGB

SOFTWARE = "MedScan v3.4.2.1 - Release"
COPYRIGHT = "(c) 2005 Trestle Corp."
LOSER = (0, 255, 0)  # the overlap a later tile covers


@dataclass
class TrestleFile:
    path: Path
    stitched: list[RGB] = field(default_factory=list)  # every level as the slide shows it
    raw: list[RGB] = field(default_factory=list)  # every level as stored


def box(a: RGB, f: int) -> RGB:
    h, w = a.shape[0] // f, a.shape[1] // f
    b = a[: h * f, : w * f].reshape(h, f, w, f, 3).mean(axis=(1, 3))
    return np.rint(b).astype(np.uint8)


def level0(w: int, h: int) -> RGB:
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:h, 0:w]
    return np.stack([xx * 255 // w, yy * 255 // h, rng.integers(0, 256, (h, w))], axis=2).astype(
        np.uint8
    )


def _tiles(img: RGB, n: tuple[int, int], step: tuple[int, int], ov: int) -> Iterator[RGB]:
    tw, th = step[0] + ov, step[1] + ov
    for r in range(n[1]):
        for c in range(n[0]):
            tile = img[r * step[1] : r * step[1] + th, c * step[0] : c * step[0] + tw].copy()
            if c < n[0] - 1:
                tile[:, step[0] :] = LOSER
            if r < n[1] - 1:
                tile[step[1] :, :] = LOSER
            yield tile


def write_trestle(
    path: Path,
    *,
    tiles: tuple[int, int] = (8, 6),
    step: tuple[int, int] = (48, 32),
    overlap: int = 32,
    overlapped: int = 2,
    coarse: int = 2,
    description: str | None = None,
    um_per_px: tuple[float, float] = (0.5, 0.5),
    level_res_scale: float = 2.0,
    coarse_margin: int | None = None,
) -> TrestleFile:
    """`overlapped` levels with tiles (overlap halving per level, same step in level pixels), then
    `coarse` levels without overlap, which leave out `coarse_margin` / 2 pixels of level 0 on each
    side (the level-0 overlap, as in CMU-1). XResolution and YResolution hold um/px, multiplied by
    `level_res_scale` per level (2 in real files)."""
    out = TrestleFile(path)
    w0, h0 = tiles[0] * step[0] + overlap, tiles[1] * step[1] + overlap
    truth = level0(w0, h0)
    pairs = " ".join(f"{overlap >> k} {overlap >> k}" for k in range(overlapped))
    if description is None:
        description = (
            "Background Color=E6E6E6;White Balance=C0AAA1;Objective Power=10;"
            f"JPEG Quality=75;OverlapsXY= {pairs}"
        )
    kw = {
        "photometric": "rgb",
        "compression": "zlib",
        "software": SOFTWARE,
        "metadata": None,
        "resolutionunit": "NONE",
        "extratags": [(33432, "s", 0, COPYRIGHT, True)],
    }
    half = (overlap if coarse_margin is None else coarse_margin) // 2
    inner = truth[half : h0 - half, half : w0 - half]
    with tifffile.TiffWriter(path) as tw:
        for k in range(overlapped + coarse):
            res = (um_per_px[0] * level_res_scale**k, um_per_px[1] * level_res_scale**k)
            extra = {"description": description} if k == 0 else {"subfiletype": 1}
            if k < overlapped:
                img = box(truth, 2**k)
                n = (tiles[0] >> k, tiles[1] >> k)
                ov = overlap >> k
                shape = (n[1] * (step[1] + ov), n[0] * (step[0] + ov), 3)
                raw = np.empty(shape, np.uint8)
                for i, tile in enumerate(_tiles(img, n, step, ov)):
                    r, c = divmod(i, n[0])
                    th, twd = tile.shape[:2]
                    raw[r * th : (r + 1) * th, c * twd : (c + 1) * twd] = tile
                tw.write(
                    _tiles(img, n, step, ov),
                    shape=shape,
                    dtype=np.uint8,
                    tile=(step[1] + ov, step[0] + ov),
                    resolution=res,
                    **extra,
                    **kw,
                )
            else:
                img = box(inner, 2**k)
                raw = img
                tw.write(img, tile=(16, 16), resolution=res, **extra, **kw)
            out.stitched.append(img)
            out.raw.append(raw)
    return out
