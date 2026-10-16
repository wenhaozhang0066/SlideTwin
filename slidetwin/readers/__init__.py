"""Slide readers (M8), one per format family, chosen by `slidetwin.core.formats.sniff`.

`open_source` opens a slide with the reader the router chose. Each reader module imports its
library at the top, so a missing library is reported with the extra to install; there is no silent
fallback to another reader. A reader module provides
`open_reader(path, match, *, name, mpp_override, options) -> SlideSource`.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point
from slidetwin.core.slide_source import ReaderOptions, SlideSource

# reader id -> (module, pip extra that installs its library, or None for the base install)
READERS: dict[str, tuple[str, str | None]] = {
    "tifffile": ("slidetwin.readers.tiff", None),
    "openslide": ("slidetwin.readers.openslide", "openslide"),
    "wsidicom": ("slidetwin.readers.dicom", None),
    "czi": ("slidetwin.readers.czi", None),
    "scn": ("slidetwin.readers.scn", None),
    "trestle": ("slidetwin.readers.trestle", None),
    "bif": ("slidetwin.readers.bif", None),
    "argos": ("slidetwin.readers.argos", None),
    "ome": ("slidetwin.readers.ome", None),
    "qptiff": ("slidetwin.readers.qptiff", None),
    "isyntax": ("slidetwin.readers.isyntax", "isyntax"),
    "slideio": ("slidetwin.readers.vsi", "slideio"),
    "zvi": ("slidetwin.readers.zvi", "slideio"),
}


def open_source(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None = None,
    options: ReaderOptions | None = None,
) -> SlideSource:
    if match.reader is None:
        raise SlideMetadataError(
            f"Slide '{name}' is in a format SlideTwin cannot read ({match.format}).",
            hint=match.reason,
        )
    spec = READERS.get(match.reader)
    if spec is None:
        raise SlideMetadataError(
            f"Slide '{name}' ({match.format}) needs the {match.reader} reader, which this version "
            "of SlideTwin does not have yet.",
            hint="Convert the slide to a pyramidal TIFF, or use a later version.",
        )
    module_name, extra = spec
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as err:
        raise SlideMetadataError(
            f"Slide '{name}' ({match.format}) needs the {match.reader} reader, whose library "
            f"({err.name}) is not installed.",
            hint=f'Install it with pip install "slidetwin[{extra}]".',
        ) from None
    opened: SlideSource = module.open_reader(
        path, match, name=name, mpp_override=mpp_override, options=options or ReaderOptions()
    )
    return opened
