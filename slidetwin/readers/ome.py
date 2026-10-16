"""OME-TIFF slides with interleaved 8-bit RGB pixels (M8): QuPath, raw2ometiff --rgb and libvips
exports, read with tifffile.

The slide is the OME image with the most pixels; other images (labels, macros, thumbnails) are
never read. Its levels are its SubIFDs. mpp comes from the image's PhysicalSizeX and PhysicalSizeY
with their units. Refused for now: multichannel, planar (one IFD per colour), z-stack and
time-series images; OME-TIFF that points into other files; and files with pages that no OME image
accounts for (raw2ometiff --legacy keeps its levels that way).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point, integer_transforms
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.tiffpages import (
    TiffPagesSource,
    checked_mpp,
    is_rgb8,
    layout_refusal,
    level_array,
    open_with,
    proportional,
    provenance,
    refusal,
)

FORMAT = "ome-tiff"
# OME length units in micrometres; any other unit (e.g. "pixel") gives no mpp.
UNIT_UM = {"m": 1e6, "cm": 1e4, "mm": 1e3, "µm": 1.0, "μm": 1.0, "nm": 1e-3, "Å": 1e-4}


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> TiffPagesSource:
    return open_with(path, name, lambda tif: _build(tif, name, mpp_override, options))


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _children(element: Any, tag: str) -> list[Any]:
    return [child for child in element if _local(child.tag) == tag]


def _main_pixels(root: Any, name: str) -> Any:
    """The Pixels element of the largest image. Images in other files are refused, since only
    this file is checked and hashed."""
    pixels = [p for image in _children(root, "Image") for p in _children(image, "Pixels")]
    uuid = root.get("UUID")
    for p in pixels:
        for data in _children(p, "TiffData"):
            if any(u.text is None or u.text.strip() != uuid for u in _children(data, "UUID")):
                raise refusal(
                    name,
                    FORMAT,
                    "refers to images in other files; SlideTwin reads single-file OME-TIFF.",
                    hint="Export the slide as one OME-TIFF file.",
                )
    areas = sorted((int(p.get("SizeX", 0)) * int(p.get("SizeY", 0)) for p in pixels), reverse=True)
    if not areas or (len(areas) > 1 and areas[0] == areas[1]):
        raise layout_refusal(name, FORMAT)  # no image, or no single largest one
    return next(p for p in pixels if int(p.get("SizeX", 0)) * int(p.get("SizeY", 0)) == areas[0])


def _mpp(pixels: Any) -> tuple[float, float] | None:
    """PhysicalSizeX/Y in um; the OME default unit is um."""
    try:
        values = [
            float(pixels.get(f"PhysicalSize{axis}"))
            * UNIT_UM[pixels.get(f"PhysicalSize{axis}Unit", "µm")]
            for axis in ("X", "Y")
        ]
    except (TypeError, ValueError, KeyError):
        return None  # missing, malformed or not a length
    return (values[0], values[1])


def _build(tif: Any, name: str, override: Point | None, options: ReaderOptions) -> TiffPagesSource:
    root = ElementTree.fromstring(tif.ome_metadata)
    pixels = _main_pixels(root, name)
    if sum(1 for series in tif.series if len(series.levels) > 1) > 1:
        raise refusal(  # serving the largest alone would hide the other tissue regions
            name,
            FORMAT,
            "holds more than one image pyramid (several regions); SlideTwin serves one per file.",
            hint="Export each region as its own OME-TIFF.",
        )
    sizes = {axis: int(pixels.get(f"Size{axis}", 0)) for axis in "XYCZT"}
    if (sizes["C"], sizes["Z"], sizes["T"], pixels.get("Type")) != (3, 1, 1, "uint8"):
        raise refusal(
            name,
            FORMAT,
            "is not an 8-bit RGB image with one plane; multichannel, z-stack and time-series "
            "OME-TIFF are not supported yet.",
            hint="Multichannel images need the channel renderer of a later version.",
        )
    series = [
        s
        for s in tif.series
        if (s.keyframe.imagewidth, s.keyframe.imagelength) == (sizes["X"], sizes["Y"])
    ]
    levels = series[0].levels if len(series) == 1 else []
    dims = [(level.keyframe.imagewidth, level.keyframe.imagelength) for level in levels]
    accounted = sum(len(s.pages) for s in tif.series)
    ok = bool(levels) and series[0].axes == "YXS" and accounted == len(tif.pages)
    ok = ok and all(is_rgb8(level.keyframe) for level in levels) and proportional(dims)
    if not ok:
        raise refusal(
            name,
            FORMAT,
            "does not store its image as interleaved RGB levels in SubIFDs, the layout "
            "SlideTwin validated.",
            hint="Export with QuPath, raw2ometiff --rgb (without --legacy) or vips --subifd.",
        )
    options.refuse_unused(name, FORMAT)
    mpp, source = checked_mpp(_mpp(pixels), "ome PhysicalSizeX/Y", override, name)
    return TiffPagesSource(
        tif,
        [level_array(level.keyframe) for level in levels],
        dims,
        mpp,
        name,
        provenance=provenance(FORMAT, source),
        transforms=integer_transforms(dims),
    )
