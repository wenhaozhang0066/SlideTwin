"""ARGOS AVS slides (M8), read with tifffile.

The pyramid is tifffile's 'Baseline' series; the Map overview and the Macro photo (which shows the
label) are other series and never read. A z-stack stores one complete pyramid per focal plane, in
IFD order, before them. Each level is floor(previous / 2) and its resolution tags are level 0's
divided by 2^k, so a level's exact scale is 2^k rather than the extent ratio. Most tiles of a scan
are missing; they are white. mpp comes from the level-0 resolution tags.

The manifest's `plane` picks a focal plane by its place in the file. Without it a z-stack shows its
middle plane, as OpenSlide does. Whether the planes run up or down in z is not recorded in the file
and was not verified, and the provenance says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.tiff import _tag_mpp
from slidetwin.readers.tiffpages import (
    TiffPagesSource,
    checked_mpp,
    choose,
    is_rgb8,
    layout_refusal,
    level_array,
    open_with,
    provenance,
)

FORMAT = "argos-avs"
DEFAULT_PLANE = "middle (default; z order not verified)"


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> TiffPagesSource:
    return open_with(path, name, lambda tif: _build(tif, name, mpp_override, options))


def _build(tif: Any, name: str, override: Point | None, options: ReaderOptions) -> TiffPagesSource:
    series = tif.series[0]
    levels = series.levels
    planes = len(levels[0].pages)
    w0, h0 = levels[0].keyframe.imagewidth, levels[0].keyframe.imagelength
    ok = series.kind == "avs" and series.name == "Baseline"
    ok = ok and series.axes == ("ZYXS" if planes > 1 else "YXS")
    for k, level in enumerate(levels):
        page = level.keyframe
        ok = ok and is_rgb8(page) and (page.imagewidth, page.imagelength) == (w0 >> k, h0 >> k)
        # one complete pyramid per plane, in IFD order
        ok = ok and [p.index for p in level.pages] == [z * len(levels) + k for z in range(planes)]
    if not ok:
        raise layout_refusal(name, FORMAT)
    options.refuse_unused(name, FORMAT, plane=True)
    plane = choose(name, FORMAT, "plane", options.plane, planes, default=(planes - 1) // 2)
    mpp, source = checked_mpp(_tag_mpp(levels[0].keyframe), "tiff resolution tags", override, name)
    settings: dict[str, Any] = {}
    if planes > 1:
        chosen = "manifest" if options.plane is not None else DEFAULT_PLANE
        settings = {"plane": plane, "planes": planes, "plane_choice": chosen}
    return TiffPagesSource(
        tif,
        [level_array(level.pages[plane]) for level in levels],
        [(w0 >> k, h0 >> k) for k in range(len(levels))],
        mpp,
        name,
        provenance=provenance(FORMAT, source, **settings),
        transforms=[LevelTransform(2.0**k, 2.0**k) for k in range(len(levels))],
    )
