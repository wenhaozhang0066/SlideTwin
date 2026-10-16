"""Shared parts of the vendor TIFF readers (M8): choices, structure checks, mpp, open and close."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.tiffpages import (
    TiffPagesSource,
    checked_mpp,
    choose,
    is_rgb8,
    open_with,
    proportional,
    require_experimental,
)


def test_choose_a_scene_or_plane() -> None:
    assert choose("s1", "fmt", "plane", None, 1) == 0  # nothing to choose
    assert choose("s1", "fmt", "plane", None, 5, default=2) == 2
    assert choose("s1", "fmt", "plane", 4, 5) == 4
    with pytest.raises(SlideMetadataError, match=r"has 3 scenes; choose one with scene: 0 to 2\."):
        choose("s1", "fmt", "scene", None, 3)
    with pytest.raises(SlideMetadataError, match=r"has no plane 5; it has 5 \(plane 0 to 4\)"):
        choose("s1", "fmt", "plane", 5, 5)


def test_proportional_levels() -> None:
    assert proportional([(1000, 600), (500, 300), (250, 150), (125, 75)])
    assert proportional([(36832, 38432), (9208, 9608), (576, 600)])  # within rounding
    assert not proportional([(1000, 600), (500, 400)])  # other proportions
    assert not proportional([(1000, 600), (1000, 600)])  # not smaller


def test_only_8_bit_interleaved_rgb_is_served(tmp_path: Path) -> None:
    pages: list[tuple[np.ndarray, dict[str, Any]]] = [
        (np.zeros((32, 32, 3), np.uint8), {"photometric": "rgb"}),
        (np.zeros((32, 32), np.uint8), {"photometric": "minisblack"}),
        (np.zeros((32, 32, 3), np.uint16), {"photometric": "rgb"}),
        (np.zeros((3, 32, 32), np.uint8), {"photometric": "rgb", "planarconfig": "separate"}),
    ]
    path = tmp_path / "a.tif"
    with tifffile.TiffWriter(path) as tw:
        for data, kw in pages:
            tw.write(data, metadata=None, **kw)
    with tifffile.TiffFile(path) as tif:
        assert [is_rgb8(page) for page in tif.pages] == [True, False, False, False]


def test_mpp_from_the_trusted_field_or_the_override() -> None:
    assert checked_mpp((0.25, 0.25), "field", None, "s1") == ((0.25, 0.25), "field")
    assert checked_mpp((0.25, 0.25), "field", (0.3, 0.3), "s1") == ((0.3, 0.3), "mpp_override")
    for raw in (None, (1000.0, 1000.0), (float("nan"), 0.25)):
        with pytest.raises(SlideMetadataError, match="refusing to guess"):
            checked_mpp(raw, "field", None, "s1")


def test_experimental_variants_need_the_flag() -> None:
    require_experimental("s1", "fmt", ReaderOptions(experimental=True), "a variant")
    with pytest.raises(SlideMetadataError, match="validated only on synthetic samples") as err:
        require_experimental("s1", "fmt", ReaderOptions(), "a variant")
    assert "experimental: true" in (err.value.hint or "")


def test_open_with_closes_the_file_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "a.tif"
    tifffile.imwrite(path, np.zeros((16, 16, 3), np.uint8), photometric="rgb")
    opened: list[Any] = []

    def broken(tif: Any) -> TiffPagesSource:
        opened.append(tif)
        raise KeyError("a name from the file")

    with pytest.raises(SlideMetadataError, match=r"'s1' could not be opened .*\(KeyError\)") as err:
        open_with(path, "s1", broken)
    assert "name from the file" not in err.value.message
    assert opened[0].filehandle.closed

    def refusing(tif: Any) -> TiffPagesSource:
        opened.append(tif)
        raise SlideMetadataError("refused")

    with pytest.raises(SlideMetadataError, match="refused"):
        open_with(path, "s1", refusing)
    assert opened[1].filehandle.closed
