"""TIFF slides read with tifffile (M8): Aperio SVS, Philips TIFF, Huron TIFF and generic pyramids.

The pyramid is tifffile's first series, read through its zarr store with missing tiles white
(tiffslide fills them black). Labels, macros and thumbnails are other series or pages and are never
opened. The mpp comes from each format's trusted field (survey section 3.8).
"""

from __future__ import annotations

import importlib.metadata
import itertools
import math
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
import zarr

from slidetwin.core.errors import SlideMetadataError, SlideTwinError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point, integer_transforms
from slidetwin.core.slide_source import (
    RGB,
    BaseSource,
    ReaderOptions,
    read_error,
    resolve_mpp,
    to_rgb,
    unreadable_error,
)

FORMATS = ("aperio-svs", "philips-tiff", "huron-tiff", "generic-tiff")
DECODE_THREADS = 4
FILL = (255, 255, 255)
# Plausible scan resolutions, um/px. Outside this range a value is a placeholder (CMU-1.tiff says
# 1000) or a screen default (72 dpi is 352.8), so the slide needs mpp_override.
MPP_RANGE = (0.05, 20.0)
_UNIT_UM = {2: 25_400.0, 3: 10_000.0}  # TIFF ResolutionUnit: inch, centimetre
_ASSOCIATED = ("label", "macro")  # how Aperio describes those pages, after the first line


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> TiffFamilySource:
    options.refuse_unused(name, match.format)
    return TiffFamilySource(path, name=name, fmt=match.format, mpp_override=mpp_override)


class TiffFamilySource(BaseSource):
    def __init__(
        self, path: Path, *, name: str, fmt: str, mpp_override: Point | None = None
    ) -> None:
        if fmt not in FORMATS:
            raise SlideMetadataError(f"Slide '{name}': the TIFF reader does not handle {fmt}.")
        try:
            tif = tifffile.TiffFile(path)
        except Exception as err:
            raise unreadable_error(name, "tifffile", err) from None
        try:
            series = tif.series[0]
            _check_structure(tif, series, name, vendor=fmt != "generic-tiff")
            dims = _level_dims(series, name)
            raw, origin = _raw_mpp(tif, fmt)
            if mpp_override is None and raw is not None and not _plausible(raw):
                raw, origin = None, f"{origin} (implausible)"
            mpp = resolve_mpp(raw[0] if raw else None, raw[1] if raw else None, mpp_override, name)
            root: Any = zarr.open(series.aszarr(fillvalue=255, maxworkers=DECODE_THREADS), mode="r")
            arrays = [root] if len(dims) == 1 else [root[str(i)] for i in range(len(dims))]
        except SlideTwinError:
            tif.close()
            raise
        except Exception as err:
            tif.close()
            raise unreadable_error(name, "tifffile", err) from None
        self._tif = tif
        self._arrays = arrays
        version = tifffile.__version__
        super().__init__(
            dims,
            mpp,
            f"tifffile {version}",
            name,
            transforms=integer_transforms(dims),
            provenance={
                "format": fmt,
                "mpp_source": "mpp_override" if mpp_override else origin,
                "series": 0,
                "fill": list(FILL),
                "versions": {
                    "tifffile": version,
                    "imagecodecs": importlib.metadata.version("imagecodecs"),
                },
            },
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        try:
            with self._lock:
                arr = np.asarray(self._arrays[level][y : y + h, x : x + w])
        except Exception as err:
            raise read_error(self._name, level, err) from None
        return to_rgb(arr)

    def close(self) -> None:
        self._tif.close()


def _level_dims(series: Any, name: str) -> list[tuple[int, int]]:
    """(width, height) per level; only 8-bit gray, RGB or RGBA single planes are served."""
    if series.axes not in ("YX", "YXS") or series.dtype != np.uint8:
        raise SlideMetadataError(
            f"Slide '{name}' stores {series.dtype} data with axes {series.axes}; SlideTwin serves "
            "8-bit gray or RGB pyramids from this format.",
            hint="Convert the slide to an 8-bit RGB pyramidal TIFF.",
        )
    if series.axes == "YXS" and series.shape[-1] not in (3, 4):
        raise SlideMetadataError(f"Slide '{name}' has {series.shape[-1]} samples per pixel.")
    return [(int(level.shape[1]), int(level.shape[0])) for level in series.levels]


def _check_structure(tif: Any, series: Any, name: str, *, vendor: bool = False) -> None:
    """A TIFF is served only if it is one clean pyramid: the levels shrink strictly with the same
    proportions (within rounding), so a label or macro taken for a level is refused, and no other
    series is a pyramid of its own (a second tissue region would be dropped silently). A generic
    TIFF must also start with its full-resolution image: IFD 0 tiled and the largest page. A
    vendor format names its base itself, and an SVS level described as a label or macro is
    refused."""
    first = tif.pages.first
    levels = series.levels
    base = levels[0]
    w0, h0 = int(base.shape[1]), int(base.shape[0])
    ok = True
    if not vendor:
        largest = max(int(p.shape[0]) * int(p.shape[1]) for p in tif.pages if p.shape)
        ok = first.is_tiled and base.keyframe.index == 0 and w0 * h0 == largest
    for prev, cur in itertools.pairwise(levels):
        w, h = int(cur.shape[1]), int(cur.shape[0])
        ok = ok and w < int(prev.shape[1]) and h < int(prev.shape[0])
        ok = ok and abs(h - h0 * w / w0) <= 1.0 + h0 / w0  # same aspect ratio, within rounding
        lines = str(getattr(cur.keyframe, "description", "") or "").splitlines()
        ok = ok and not (len(lines) > 1 and lines[1].strip().lower().startswith(_ASSOCIATED))
    ok = ok and not any(len(other.levels) > 1 for other in tif.series[1:])
    if not ok:
        raise SlideMetadataError(
            f"Slide '{name}' is a TIFF whose pages are not one clean pyramid; SlideTwin will not "
            "guess which page is the slide.",
            hint="Save the slide as a pyramidal TIFF with the full-resolution image first.",
        )


def _raw_mpp(tif: Any, fmt: str) -> tuple[tuple[float, float] | None, str]:
    """The format's trusted resolution field, um/px (x, y), and where it came from."""
    if fmt == "aperio-svs":
        from tifffile.tifffile import svs_description_metadata

        value = svs_description_metadata(tif.pages.first.description).get("MPP")
        return ((float(value), float(value)) if value is not None else None), "svs MPP"
    if fmt == "philips-tiff":
        return _philips_mpp(tif.philips_metadata), "philips DICOM_PIXEL_SPACING"
    return _tag_mpp(tif.pages.first), "tiff resolution tags"


def _philips_mpp(xml: str) -> tuple[float, float] | None:
    """The first DICOM_PIXEL_SPACING (level 0): row then column spacing in mm (as tiffslide)."""
    from xml.etree import ElementTree

    nodes = ElementTree.fromstring(xml).iterfind(".//Attribute[@Name='DICOM_PIXEL_SPACING']")
    node = next(nodes, None)
    values = [
        float(v.strip('"')) * 1000 for v in ((node.text or "") if node is not None else "").split()
    ]
    return (values[1], values[0]) if len(values) >= 2 else None


def _tag_mpp(page: Any) -> tuple[float, float] | None:
    per_um = _UNIT_UM.get(int(page.tags.valueof("ResolutionUnit") or 1))
    xres, yres = page.tags.valueof("XResolution"), page.tags.valueof("YResolution")
    if per_um is None or not xres or not yres:
        return None  # no unit (NONE) or no resolution: never guess
    rx, ry = xres[0] / xres[1], yres[0] / yres[1]
    if rx <= 0 or ry <= 0:
        return None
    return (per_um / rx, per_um / ry)


def _plausible(mpp: tuple[float, float]) -> bool:
    lo, hi = MPP_RANGE
    return all(math.isfinite(v) and lo <= v <= hi for v in mpp)
