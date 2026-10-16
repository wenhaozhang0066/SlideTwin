"""OpenSlide formats (M8): Hamamatsu NDPI and VMS/VMU, 3DHistech MIRAX, Sakura SVSlide.

Uses the OpenSlide source of earlier versions; the hardening of M8 (cached downsamples, reopening
after a read error, file sets) builds on it.
"""

from __future__ import annotations

from pathlib import Path

import openslide  # noqa: F401  (a missing library is reported with the extra to install)

from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point
from slidetwin.core.slide_source import OpenSlideSource, ReaderOptions, SlideSource


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> SlideSource:
    options.refuse_unused(name, match.format)
    return OpenSlideSource(path, name=name, mpp_override=mpp_override)
