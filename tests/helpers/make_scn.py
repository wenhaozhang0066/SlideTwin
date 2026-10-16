"""Small Leica SCN files from synthetic pixels (ported from the M8 survey's make_scn_fixture.py).

Layout as in the real samples: little-endian BigTIFF, SCN 2010/10/01 XML in IFD 0, the macro image
first (view offset 0 and view size equal to the collection), then one image per region (ROI) with
levels 4x apart, one IFD per level and focal plane (brightfield) or per level and channel
(fluorescence). The collection name, image names and barcode are marked SECRET so that tests can
check they never leak.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from slidetwin.core.slide_source import RGB

NS = "http://www.leica-microsystems.com/scn/2010/10/01"
COLLECTION_NM = (12_000_000, 30_000_000)  # 12 mm x 30 mm of glass
MACRO_NM_PX = 100_000  # 100 um/px: a 120 x 300 px macro


@dataclass
class Roi:
    w: int = 1024
    h: int = 768
    nm_per_px: int = 500
    offset_nm: tuple[int, int] = (2_000_000, 7_000_000)
    planes: int = 1
    levels: int = 3
    seed: int = 0


@dataclass
class ScnFile:
    path: Path
    # truth[roi][plane][level] as written (lossless compression)
    truth: list[list[list[RGB]]] = field(default_factory=list)


def roi_image(w: int, h: int, seed: int) -> RGB:
    """Pink background, purple disks, black grid lines every 64 px and a dark bar per seed."""
    rng = np.random.default_rng(seed)
    img = np.empty((h, w, 3), np.uint8)
    img[:] = (238, 214, 230)
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(12):
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(8, 40)
        img[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = (120, 60, 150)
    img[:, ::64] = 0
    img[::64, :] = 0
    img[8:24, 8 : 16 + 8 * seed] = (40, 20, 60)
    return img


def down4(a: np.ndarray) -> np.ndarray:
    """A 4x block mean (Leica levels are 4x apart)."""
    h, w = a.shape[0] // 4, a.shape[1] // 4
    b = a[: h * 4, : w * 4].reshape(h, 4, w, 4, *a.shape[2:]).mean(axis=(1, 3))
    return np.rint(b).astype(np.uint8)


def _image_xml(
    index: int,
    dims: list[str],
    size: tuple[int, int],
    view: tuple[int, int, int, int],
    illumination: str,
    channels: str = "",
) -> str:
    vw, vh, ox, oy = view
    return (
        f'<image name="SECRET-IMAGE-{index}" uuid="urn:uuid:00000000-0000-0000-0000-{index:012d}">'
        "<creationDate>2026-01-01T00:00:00Z</creationDate>"
        '<device model="Synthetic SCN;Synthetic" version="1.0"/>'
        f'<pixels sizeX="{size[0]}" sizeY="{size[1]}">{"".join(dims)}</pixels>'
        f'<view sizeX="{vw}" sizeY="{vh}" offsetX="{ox}" offsetY="{oy}" spacingZ="400"/>'
        "<scanSettings><objectiveSettings><objective>20</objective></objectiveSettings>"
        "<illuminationSettings><numericalAperture>0.4</numericalAperture>"
        f"<illuminationSource>{illumination}</illuminationSource></illuminationSettings>"
        f"{channels}</scanSettings></image>"
    )


def write_scn(
    path: Path,
    rois: list[Roi],
    *,
    fluorescence: bool = False,
    namespace: str = NS,
    compression: str = "zlib",
    tag_scale: float = 1.0,
    edit_xml: Callable[[str], str] | None = None,
) -> ScnFile:
    """Write an SCN file. `tag_scale` multiplies the um/px in the regions' resolution tags (to make
    them disagree with the XML); `edit_xml` changes the XML text before it is written."""
    out = ScnFile(path)
    pages: list[tuple[np.ndarray, str, float]] = []  # (array, photometric, um/px)
    images: list[str] = []
    level = np.full(
        (COLLECTION_NM[1] // MACRO_NM_PX, COLLECTION_NM[0] // MACRO_NM_PX, 3), 205, np.uint8
    )
    level[: level.shape[0] // 5] = (90, 90, 160)  # the label end: flat colour, no text
    size = (level.shape[1], level.shape[0])
    dims = []
    for r in range(2):
        dims.append(
            f'<dimension sizeX="{level.shape[1]}" sizeY="{level.shape[0]}" r="{r}" ifd="{r}"/>'
        )
        pages.append((level, "rgb", MACRO_NM_PX / 1000 * 4**r))
        level = down4(level)
    images.append(_image_xml(0, dims, size, (*COLLECTION_NM, 0, 0), "brightfield"))
    for i, roi in enumerate(rois, start=1):
        dims = []
        planes: list[list[RGB]] = []
        if fluorescence:
            chans = np.stack([roi_image(roi.w, roi.h, roi.seed + c)[:, :, c] for c in range(3)])
            for r in range(roi.levels):
                for c in range(3):
                    dims.append(
                        f'<dimension sizeX="{chans.shape[2]}" sizeY="{chans.shape[1]}" r="{r}" '
                        f'c="{c}" ifd="{len(pages)}"/>'
                    )
                    pages.append((chans[c], "minisblack", roi.nm_per_px / 1000 * 4**r * tag_scale))
                chans = np.stack([down4(ch) for ch in chans])
        else:
            for z in range(roi.planes):
                level = roi_image(roi.w, roi.h, roi.seed)
                if z:
                    level = (255 - level).astype(np.uint8)  # another plane, easy to tell apart
                levels = []
                for r in range(roi.levels):
                    zattr = f' z="{z}"' if roi.planes > 1 else ""
                    dims.append(
                        f'<dimension sizeX="{level.shape[1]}" sizeY="{level.shape[0]}" r="{r}"'
                        f'{zattr} ifd="{len(pages)}"/>'
                    )
                    pages.append((level, "rgb", roi.nm_per_px / 1000 * 4**r * tag_scale))
                    levels.append(level)
                    level = down4(level)
                planes.append(levels)
        out.truth.append(planes)
        channels = ""
        if fluorescence:
            channels = (
                "<channelSettings>"
                '<channel index="0" name="DAPI" rgb="#0000ff"/>'
                '<channel index="1" name="FITC" rgb="#00ff00"/>'
                '<channel index="2" name="TxRed" rgb="#ff0000"/>'
                "</channelSettings>"
            )
        view = (roi.w * roi.nm_per_px, roi.h * roi.nm_per_px, *roi.offset_nm)
        illumination = "fluorescence" if fluorescence else "brightfield"
        images.append(_image_xml(i, dims, (roi.w, roi.h), view, illumination, channels))
    barcode = base64.b64encode(b"SECRET-BARCODE").decode()
    xml = (
        f'<?xml version="1.0"?>\n<scn xmlns="{namespace}" uuid="urn:uuid:0">'
        f'<collection name="SECRET-COLLECTION" uuid="urn:uuid:1" sizeX="{COLLECTION_NM[0]}" '
        f'sizeY="{COLLECTION_NM[1]}"><barcode>{barcode}</barcode>{"".join(images)}'
        "</collection></scn>"
    )
    if edit_xml is not None:
        xml = edit_xml(xml)
    with tifffile.TiffWriter(path, bigtiff=True, byteorder="<") as tw:
        for n, (arr, photometric, umpp) in enumerate(pages):
            tw.write(
                arr,
                tile=(256, 256),
                compression=compression,
                photometric=photometric,
                resolution=(10_000 / umpp, 10_000 / umpp),
                resolutionunit="CENTIMETER",
                description=xml if n == 0 else None,
                metadata=None,
            )
    return out
