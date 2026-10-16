"""Akoya QPTIFF reader (M8): pages by ImageType, brightfield only, mpp from the resolution tags."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.qptiff import open_reader
from slidetwin.readers.tiffpages import TiffPagesSource
from tests.helpers.make_ome import rgb_levels, write_qptiff

MATCH = FormatMatch("akoya-qptiff", "qptiff")


def open_qptiff(path: Path, **kw: Any) -> TiffPagesSource:
    kw.setdefault("mpp_override", None)
    options = kw.pop("options", ReaderOptions())
    return open_reader(path, MATCH, name="s1", options=options, **kw)


def test_brightfield_pyramid(tmp_path: Path) -> None:
    path = tmp_path / "a.qptiff"
    levels = write_qptiff(path)
    assert sniff(path) == MATCH
    src = open_source(path, sniff(path), name="s1")
    assert isinstance(src, TiffPagesSource)
    assert src.level_dimensions == ((800, 600), (400, 300), (200, 150))  # thumbnail left out
    assert src.mpp == pytest.approx((0.4989, 0.4989))
    for k, level in enumerate(levels):  # the last level is stored in strips
        w, h = src.level_dimensions[k]
        np.testing.assert_array_equal(src.read_level(k, 0, 0, w, h), level)
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"]) == ("akoya-qptiff", "qptiff resolution tags")
    assert "SECRET" not in json.dumps(prov)
    src.close()


@pytest.mark.parametrize("options", [ReaderOptions(), ReaderOptions(channels=(0,))])
def test_fluorescence_is_refused_for_now(tmp_path: Path, options: ReaderOptions) -> None:
    path = tmp_path / "f.qptiff"
    write_qptiff(path, channels=3)
    with pytest.raises(SlideMetadataError, match="fluorescence scans and unmixed comp") as err:
        open_qptiff(path, options=options)
    assert "SECRET" not in err.value.message


def test_pyramid_pages_must_be_rgb_and_shrink(tmp_path: Path) -> None:
    odd = [*rgb_levels(), rgb_levels(300, 300, 1)[0]]  # a "reduced" level with other proportions
    write_qptiff(tmp_path / "odd.qptiff", odd)
    with pytest.raises(SlideMetadataError, match="layout SlideTwin validated"):
        open_qptiff(tmp_path / "odd.qptiff")
    two = tmp_path / "two.qptiff"
    write_qptiff(two, kinds=["FullResolution", "FullResolution", "ReducedResolution"])
    with pytest.raises(SlideMetadataError, match="not a brightfield RGB scan"):
        open_qptiff(two)


def test_no_scene_plane_or_channels(tmp_path: Path) -> None:
    path = tmp_path / "a.qptiff"
    write_qptiff(path)
    with pytest.raises(SlideMetadataError, match="has no scene to choose"):
        open_qptiff(path, options=ReaderOptions(scene=0))


def test_mpp_needs_the_resolution_tags(tmp_path: Path) -> None:
    path = tmp_path / "a.qptiff"
    write_qptiff(path)
    with tifffile.TiffFile(path, mode="r+b") as tif:
        tif.pages[0].tags["ResolutionUnit"].overwrite(1)  # NONE; the nominal 0.5 is not used
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_qptiff(path)
    assert open_qptiff(path, mpp_override=(0.5, 0.5)).provenance["mpp_source"] == "mpp_override"
