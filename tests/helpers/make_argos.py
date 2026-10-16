"""Small ARGOS AVS files from synthetic pixels (after the M8 survey's argos make_fixture.py).

Layout as in Argos-1.avs: a BigTIFF with one complete pyramid per focal plane in IFD order (each
level floor(previous / 2), resolution tags halving), then the Map overview and the Macro photo as
stripped IFDs. The Argos XML sits in tag 65000 of IFD 0 and carries a SECRET barcode. Some tiles
are dropped (offset and byte count 0), as in the sparse real scans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from slidetwin.core.slide_source import RGB

TILE = 128
XML = (
    "<Argos.Scan.Metadata><ObjectiveMagnification>20</ObjectiveMagnification>"
    "<AcquisitionDate>01.23.2024</AcquisitionDate><AcquisitionTime>10:57:59</AcquisitionTime>"
    "<ZRange>{zrange}</ZRange><MinZ>{minz}</MinZ><MaxZ>{maxz}</MaxZ>"
    "<Barcode>$SECRET-BARCODE</Barcode></Argos.Scan.Metadata>"
)


@dataclass
class ArgosFile:
    path: Path
    truth: list[list[RGB]] = field(default_factory=list)  # truth[plane][level], missing tiles white
    dropped: list[tuple[int, int, int]] = field(default_factory=list)  # (level, col, row)


def plane_image(w: int, h: int, plane: int) -> RGB:
    rng = np.random.default_rng(10 + plane)
    img = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    img[:, :, 0] = 40 * (plane + 1)  # each plane has its own red level
    return img


def halve(a: RGB) -> RGB:
    h, w = a.shape[0] // 2, a.shape[1] // 2
    b = a[: 2 * h, : 2 * w].reshape(h, 2, w, 2, 3).mean(axis=(1, 3))
    return np.rint(b).astype(np.uint8)


def write_argos(
    path: Path,
    *,
    size: tuple[int, int] = (1100, 700),
    planes: int = 1,
    levels: int = 3,
    um_per_px: float = 0.25,
    dropped: tuple[tuple[int, int, int], ...] = ((0, 1, 0), (0, 0, 2), (1, 1, 1)),
    associated: bool = True,
) -> ArgosFile:
    out = ArgosFile(path, dropped=list(dropped))
    xml = XML.format(zrange=2 * (planes - 1), minz=-(planes // 2), maxz=planes // 2)
    res0 = 1e4 / um_per_px
    with tifffile.TiffWriter(path, bigtiff=True) as tw:
        for z in range(planes):
            level = plane_image(*size, z)
            pyramid = []
            for k in range(levels):
                tw.write(
                    level,
                    tile=(TILE, TILE),
                    compression="zlib",
                    photometric="rgb",
                    resolution=(res0 / 2**k, res0 / 2**k),
                    resolutionunit="CENTIMETER",
                    metadata=None,
                    software=False,
                    extratags=[(65000, "s", 0, xml, True)] if (z == 0 and k == 0) else None,
                )
                pyramid.append(level)
                level = halve(level)
            out.truth.append(pyramid)
        if associated:
            for img, res in (
                (np.full((40, 90, 3), 225), 636.22),
                (np.full((30, 80, 3), 90), 203.1),
            ):
                tw.write(
                    img.astype(np.uint8),
                    rowsperstrip=16,
                    compression="zlib",
                    photometric="rgb",
                    resolution=(res, res),
                    resolutionunit="CENTIMETER",
                    metadata=None,
                    software=False,
                )
    with tifffile.TiffFile(path, mode="r+b") as tif:
        for z in range(planes):
            for k in range(levels):
                page = tif.pages[z * levels + k]
                across = -(-page.imagewidth // TILE)
                offsets = list(page.tags["TileOffsets"].value)
                counts = list(page.tags["TileByteCounts"].value)
                for c, r in [(c, r) for kk, c, r in dropped if kk == k]:
                    offsets[r * across + c] = counts[r * across + c] = 0
                    out.truth[z][k][r * TILE : (r + 1) * TILE, c * TILE : (c + 1) * TILE] = 255
                page.tags["TileOffsets"].overwrite(offsets)
                page.tags["TileByteCounts"].overwrite(counts)
    return out
