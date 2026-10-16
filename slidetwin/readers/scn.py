"""Leica SCN slides (M8): brightfield regions, read with tifffile.

The SCN XML in IFD 0 lists the images of the slide. An image whose view covers the whole
collection is a macro (the whole glass, label included) and is never read; every other image is a
region of interest (ROI), served as one scene in its own frame. Each level of a region is one IFD
per focal plane, named by a `dimension` element. The levels span the region's view, so the XML's
nm/px per level is the extent ratio (D13). mpp is the view size in nm over the level-0 size in
pixels, cross-checked against the level-0 resolution tags.

Without `experimental`, only the validated case is served: one brightfield region, one focal plane,
the 2010/10/01 XML. Several regions (also at mixed resolutions) and z-stacks were validated only on
synthetic files; a file with several regions must name its `scene`, a z-stack its `plane`.
Fluorescence SCN is refused for now: it needs the channel renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point, Size
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
    proportional,
    provenance,
    refusal,
    require_experimental,
)

FORMAT = "leica-scn"
NAMESPACE = "http://www.leica-microsystems.com/scn/2010/10/01"  # the version of the samples
MPP_SOURCE = "scn view nm / level-0 px"
# The XML and the resolution tags must agree this closely, else neither is trusted.
MPP_AGREEMENT = 0.01


@dataclass
class Region:
    """A non-macro image of the collection: its view (nm) and its IFDs and their sizes by
    (level, plane, channel)."""

    view: Size
    illumination: str
    ifds: dict[tuple[int, int, int], int] = field(default_factory=dict)
    sizes: dict[tuple[int, int, int], Size] = field(default_factory=dict)


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> TiffPagesSource:
    return open_with(path, name, lambda tif: _build(tif, name, mpp_override, options))


def _q(tag: str) -> str:
    return f"{{{NAMESPACE}}}{tag}"


def regions(xml: str, name: str) -> list[Region]:
    """The collection's regions in XML order. Macros are left out by the rule OpenSlide uses."""
    root = ElementTree.fromstring(xml)
    collections = root.findall(_q("collection"))
    if root.tag != _q("scn") or len(collections) != 1:
        raise refusal(
            name,
            FORMAT,
            "has SCN XML of a version or structure SlideTwin has not validated.",
            hint="SlideTwin reads SCN files with one collection in the 2010/10/01 XML.",
        )
    glass = (int(collections[0].get("sizeX", -1)), int(collections[0].get("sizeY", -1)))
    tags = ("scanSettings", "illuminationSettings", "illuminationSource")
    source_path = "/".join(_q(t) for t in tags)
    out = []
    for image in collections[0].findall(_q("image")):
        view = image.find(_q("view"))
        pixels = image.find(_q("pixels"))
        if view is None or pixels is None:
            raise layout_refusal(name, FORMAT)
        size = (int(view.get("sizeX", 0)), int(view.get("sizeY", 0)))
        offset = (int(view.get("offsetX", -1)), int(view.get("offsetY", -1)))
        if offset == (0, 0) and size == glass:
            continue  # a macro: the whole glass slide, label included
        region = Region(size, (image.findtext(source_path) or "").strip())
        for dim in pixels.findall(_q("dimension")):
            key = (int(dim.get("r", 0)), int(dim.get("z", 0)), int(dim.get("c", 0)))
            if key in region.ifds:
                raise layout_refusal(name, FORMAT)
            region.ifds[key] = int(dim.get("ifd", -1))
            region.sizes[key] = (int(dim.get("sizeX", 0)), int(dim.get("sizeY", 0)))
        out.append(region)
    return out


def _pyramid(tif: Any, region: Region, name: str) -> tuple[dict[tuple[int, int], Any], list[Size]]:
    """The region's pages by (level, plane) and its level sizes: every level has every plane and
    one channel, each page is 8-bit RGB of the size the XML gives, and the levels shrink."""
    levels = 1 + max((r for r, _, _ in region.ifds), default=-1)
    planes = 1 + max((z for _, z, _ in region.ifds), default=-1)
    grid = {(r, z, 0) for r in range(levels) for z in range(planes)}
    count = len(tif.pages)
    ok = levels > 0 and region.illumination == "brightfield" and set(region.ifds) == grid
    ok = ok and all(0 <= i < count for i in region.ifds.values())
    pages = {(r, z): tif.pages[ifd] for (r, z, _), ifd in region.ifds.items()} if ok else {}
    for (r, z), page in pages.items():
        size = region.sizes[(r, z, 0)]
        ok = ok and is_rgb8(page) and (page.imagewidth, page.imagelength) == size
    dims = [region.sizes.get((r, 0, 0), (0, 0)) for r in range(levels)]
    if not (ok and proportional(dims)):
        raise layout_refusal(name, FORMAT)
    return pages, dims


def _build(tif: Any, name: str, override: Point | None, options: ReaderOptions) -> TiffPagesSource:
    found = regions(tif.pages.first.description, name)
    if any(r.illumination == "fluorescence" for r in found):
        raise refusal(
            name,
            FORMAT,
            "is a fluorescence scan; SlideTwin serves brightfield SCN only for now.",
            hint="Fluorescence needs the channel renderer of a later version.",
        )
    if not found:
        raise layout_refusal(name, FORMAT)
    options.refuse_unused(name, FORMAT, scene=True, plane=True)
    scene = choose(name, FORMAT, "scene", options.scene, len(found))
    pages, dims = _pyramid(tif, found[scene], name)
    planes = 1 + max(z for _, z in pages)
    plane = choose(name, FORMAT, "plane", options.plane, planes)
    if len(found) > 1:
        require_experimental(name, FORMAT, options, "a file with several regions")
    if planes > 1:
        require_experimental(name, FORMAT, options, "a z-stack")
    view = found[scene].view
    raw = (view[0] / dims[0][0] / 1000, view[1] / dims[0][1] / 1000)
    tags = _tag_mpp(pages[(0, 0)])
    if tags is not None and any(
        abs(t - v) > MPP_AGREEMENT * v for t, v in zip(tags, raw, strict=True)
    ):
        raw = (float("nan"), float("nan"))  # the two sources disagree: trust neither
    mpp, source = checked_mpp(raw, MPP_SOURCE, override, name)
    settings: dict[str, Any] = {}
    if len(found) > 1:
        settings |= {"scene": scene, "scenes": len(found)}
    if planes > 1:
        settings |= {"plane": plane, "planes": planes}
    if settings:
        settings["experimental"] = True
    return TiffPagesSource(
        tif,
        [level_array(pages[(r, plane)]) for r in range(len(dims))],
        dims,
        mpp,
        name,
        provenance=provenance(FORMAT, source, **settings),
    )
