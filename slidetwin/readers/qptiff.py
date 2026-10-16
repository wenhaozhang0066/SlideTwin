"""Akoya (PerkinElmer) QPTIFF brightfield slides (M8), read with tifffile.

Every IFD names its role in its QPI XML (ImageType). The pyramid is the one FullResolution IFD and
the ReducedResolution IFDs after it, in file order; the thumbnail, overview and label are never
read. The levels are checked by their sizes, not assumed to halve (tifffile's QPI series truncates
pyramids that do not). Brightfield scans store 8-bit RGB; fluorescence scans store one grey IFD per
channel and are refused for now, as are inForm's unmixed component images (float, one field).
mpp comes from the full-resolution resolution tags, not from the nominal ScanProfile pixel size.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point, integer_transforms
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.tiff import _tag_mpp
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

FORMAT = "akoya-qptiff"
_TYPE = re.compile(r"<ImageType>\s*(\w+)\s*</ImageType>")


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> TiffPagesSource:
    return open_with(path, name, lambda tif: _build(tif, name, mpp_override, options))


def image_type(page: Any) -> str | None:
    """The page's QPI ImageType (only that is read from the XML)."""
    found = _TYPE.search(page.description or "")
    return found.group(1) if found else None


def _build(tif: Any, name: str, override: Point | None, options: ReaderOptions) -> TiffPagesSource:
    pages = list(tif.pages)
    kinds = [image_type(page) for page in pages]
    full = [page for page, kind in zip(pages, kinds, strict=True) if kind == "FullResolution"]
    if len(full) != 1 or not is_rgb8(full[0]):
        raise refusal(
            name,
            FORMAT,
            "is not a brightfield RGB scan; fluorescence scans and unmixed component images "
            "are not supported yet.",
            hint="Fluorescence needs the channel renderer of a later version.",
        )
    first = pages.index(full[0])
    reduced = [
        p for p, k in zip(pages[first:], kinds[first:], strict=True) if k == "ReducedResolution"
    ]
    levels = [full[0], *reduced]
    dims = [(page.imagewidth, page.imagelength) for page in levels]
    if not (all(is_rgb8(page) for page in levels) and proportional(dims)):
        raise layout_refusal(name, FORMAT)
    options.refuse_unused(name, FORMAT)
    mpp, source = checked_mpp(_tag_mpp(full[0]), "qptiff resolution tags", override, name)
    return TiffPagesSource(
        tif,
        [level_array(page) for page in levels],
        dims,
        mpp,
        name,
        provenance=provenance(FORMAT, source),
        transforms=integer_transforms(dims),
    )
