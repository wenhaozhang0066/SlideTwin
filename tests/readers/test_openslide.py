"""OpenSlide formats (M8): the reader entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.openslide import open_reader
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff

MATCH = FormatMatch("hamamatsu-ndpi", "openslide")


def test_opens_with_openslide(tmp_path: Path) -> None:
    path = write_pyramid_tiff(tmp_path / "s.tif", pyramid(line_image(256, 256, (10,), ()), 2), 0.5)
    src = open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions())
    assert src.backend.startswith("openslide")
    assert src.mpp == pytest.approx((0.5, 0.5))
    with pytest.raises(SlideMetadataError, match="has no scene to choose"):
        open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions(scene=1))
