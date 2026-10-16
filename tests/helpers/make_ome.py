"""Small OME-TIFF and QPTIFF files from synthetic pixels.

OME-TIFF: the layout of QuPath, raw2ometiff --rgb and libvips exports (interleaved RGB, levels in
SubIFDs), written by tifffile, plus the variants a reader must refuse. QPTIFF: the Akoya layout,
every IFD with a QPI XML description naming its image type.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from slidetwin.core.slide_source import RGB


def rgb_levels(w: int = 800, h: int = 600, n: int = 3, seed: int = 0) -> list[RGB]:
    rng = np.random.default_rng(seed)
    levels = [rng.integers(0, 256, (h, w, 3), dtype=np.uint8)]
    for _ in range(1, n):
        a = levels[-1]
        hh, ww = a.shape[0] // 2, a.shape[1] // 2
        b = a[: 2 * hh, : 2 * ww].reshape(hh, 2, ww, 2, 3).mean(axis=(1, 3))
        levels.append(np.rint(b).astype(np.uint8))
    return levels


def write_ome(
    path: Path,
    levels: Sequence[np.ndarray] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    photometric: str = "rgb",
    planarconfig: str | None = None,
    extra_images: Sequence[np.ndarray] = (),
    extra_pages: Sequence[np.ndarray] = (),
    byteorder: str = "<",
) -> list[np.ndarray]:
    """An OME-TIFF with `levels` as level 0 and its SubIFDs. `extra_images` become further OME
    images (labels, macros); `extra_pages` are written after them without any OME image (the
    raw2ometiff --legacy layout)."""
    levels = list(rgb_levels() if levels is None else levels)
    meta: dict[str, Any] = {"axes": "YXS", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5}
    meta.update(metadata or {})
    meta.setdefault("Name", "SECRET-IMAGE")
    kw: dict[str, Any] = {"tile": (256, 256), "compression": "zlib", "photometric": photometric}
    if planarconfig is not None:
        kw["planarconfig"] = planarconfig
    with tifffile.TiffWriter(path, bigtiff=True, ome=True, byteorder=byteorder) as tw:
        tw.write(levels[0], subifds=len(levels) - 1, metadata=meta, **kw)
        for level in levels[1:]:
            tw.write(level, subfiletype=1, **kw)
        for img in extra_images:
            tw.write(img, photometric="rgb", metadata={"axes": "YXS", "Name": "SECRET-LABEL"})
        for img in extra_pages:
            tw.write(img, photometric="rgb", metadata={"axes": "YXS", "Name": "UNLISTED"})
    if extra_pages:  # remove their OME images, so that no image names these pages
        unlisted = re.compile(r'<Image ID="Image:\d+" Name="UNLISTED">.*?</Image>')
        set_description(path, lambda xml: unlisted.sub("", xml))
    return levels


def set_description(path: Path, edit: Any) -> None:
    """Rewrite the ImageDescription of IFD 0 (the OME-XML) with `edit(text)`."""
    with tifffile.TiffFile(path) as tif:
        text = tif.pages.first.description
    tifffile.tiffcomment(path, edit(text))


QPI = (
    '<?xml version="1.0" encoding="utf-16"?>\r\n<PerkinElmer-QPI-ImageDescription>\r\n'
    "  <DescriptionVersion>2</DescriptionVersion>\r\n"
    "  <AcquisitionSoftware>VectraPolaris 1.0</AcquisitionSoftware>\r\n"
    "  <ImageType>{kind}</ImageType>\r\n  <SlideID>SECRET-SLIDE</SlideID>\r\n"
    "  <Barcode>SECRET-BARCODE</Barcode>\r\n  <Name>{name}</Name>\r\n"
    "  <ScanProfile><PixelSizeMicrons>0.5</PixelSizeMicrons></ScanProfile>\r\n"
    "</PerkinElmer-QPI-ImageDescription>"
)


def write_qptiff(
    path: Path,
    levels: Sequence[np.ndarray] | None = None,
    *,
    channels: int = 0,
    um_per_px: float = 0.4989,
    kinds: Sequence[str] | None = None,
    strip_last: bool = True,
) -> list[np.ndarray]:
    """A QPTIFF: brightfield (RGB levels) or, with `channels`, fluorescence (one grey IFD per
    channel and level). The thumbnail follows the full-resolution image, and the overview and
    label come last, as Akoya writes them. `kinds` overrides the ImageType of each pyramid IFD."""
    levels = list(rgb_levels() if levels is None else levels)
    pages: list[tuple[np.ndarray, str, float]] = []  # (pixels, image type, um/px)
    for k, level in enumerate(levels):
        kind = "FullResolution" if k == 0 else "ReducedResolution"
        if channels:
            pages += [(level[:, :, c % 3], kind, um_per_px * 2**k) for c in range(channels)]
        else:
            pages.append((level, kind, um_per_px * 2**k))
        if k == 0:
            pages.append((levels[-1], "Thumbnail", um_per_px * 2 ** len(levels)))
    pages.append((np.full((80, 40, 3), 200, np.uint8), "Overview", 40.0))
    pages.append((np.full((30, 30, 3), 60, np.uint8), "Label", 40.0))
    if kinds is not None:
        pyramid = [i for i, p in enumerate(pages) if p[1].endswith("Resolution")]
        for i, kind in zip(pyramid, kinds, strict=True):
            pages[i] = (pages[i][0], kind, pages[i][2])
    last_level = (
        max(i for i, p in enumerate(pages) if p[1] == "ReducedResolution")
        if len(levels) > 1
        else -1
    )
    with tifffile.TiffWriter(path, bigtiff=True) as tw:
        for i, (img, kind, umpp) in enumerate(pages):
            tiled = kind.endswith("Resolution") and not (strip_last and i == last_level)
            tw.write(
                img,
                photometric="rgb" if img.ndim == 3 else "minisblack",
                compression="zlib",
                tile=(256, 256) if tiled else None,
                rowsperstrip=None if tiled else 64,
                description=QPI.format(kind=kind, name="SECRET-CHANNEL" if channels else "BF"),
                software="PerkinElmer-QPI",
                resolution=(1e4 / umpp, 1e4 / umpp),
                resolutionunit="CENTIMETER",
                metadata=None,
            )
    return levels
