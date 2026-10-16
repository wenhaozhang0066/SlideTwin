"""Slide readers (M8), one per format family, chosen by `slidetwin.core.formats.sniff`.

`open_source` opens a slide with the reader the router chose. A reader whose library is not
installed is refused with the extra to install; there is no silent fallback to another reader.
"""

from __future__ import annotations

from pathlib import Path

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import Point
from slidetwin.core.slide_source import SlideSource

# Readers that exist in this version; the others named by the router come later in M8.
AVAILABLE = ("tifffile", "openslide")


def open_source(
    path: Path, match: FormatMatch, *, name: str, mpp_override: Point | None = None
) -> SlideSource:
    if match.reader is None:
        raise SlideMetadataError(
            f"Slide '{name}' is in a format SlideTwin cannot read ({match.format}).",
            hint=match.reason,
        )
    if match.reader == "tifffile":
        from slidetwin.readers.tiff import TiffFamilySource

        return TiffFamilySource(path, name=name, fmt=match.format, mpp_override=mpp_override)
    if match.reader == "openslide":
        from slidetwin.core.slide_source import OpenSlideSource

        try:
            return OpenSlideSource(path, name=name, mpp_override=mpp_override)
        except ModuleNotFoundError:
            raise SlideMetadataError(
                f"Slide '{name}' ({match.format}) needs OpenSlide, which is not installed.",
                hint='Install it with pip install "slidetwin[openslide]".',
            ) from None
    raise SlideMetadataError(
        f"Slide '{name}' ({match.format}) needs the {match.reader} reader, which this version of "
        "SlideTwin does not have yet.",
        hint="Convert the slide to a pyramidal TIFF, or use a later version.",
    )
