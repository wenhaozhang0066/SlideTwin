"""OME-TIFF reader (M8): interleaved RGB pyramids in SubIFDs, the largest image only, OME mpp."""

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
from slidetwin.readers.ome import open_reader
from slidetwin.readers.tiffpages import TiffPagesSource
from tests.helpers.make_ome import rgb_levels, set_description, write_ome

MATCH = FormatMatch("ome-tiff", "ome")


def open_ome(path: Path, **kw: Any) -> TiffPagesSource:
    kw.setdefault("mpp_override", None)
    options = kw.pop("options", ReaderOptions())
    return open_reader(path, MATCH, name="s1", options=options, **kw)


def full(src: TiffPagesSource, level: int) -> np.ndarray:
    w, h = src.level_dimensions[level]
    return src.read_level(level, 0, 0, w, h)


@pytest.mark.parametrize("byteorder", ["<", ">"])  # QuPath writes big-endian files
def test_rgb_pyramid_in_subifds(tmp_path: Path, byteorder: str) -> None:
    path = tmp_path / "a.ome.tif"
    levels = write_ome(path, byteorder=byteorder, extra_images=[np.zeros((50, 60, 3), np.uint8)])
    assert sniff(path) == MATCH
    src = open_source(path, sniff(path), name="s1")
    assert isinstance(src, TiffPagesSource)
    assert src.level_dimensions == ((800, 600), (400, 300), (200, 150))  # not the label image
    assert src.mpp == pytest.approx((0.5, 0.5))
    for k, level in enumerate(levels):
        np.testing.assert_array_equal(full(src, k), level)
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"]) == ("ome-tiff", "ome PhysicalSizeX/Y")
    assert "SECRET" not in json.dumps(prov)
    src.close()


@pytest.mark.parametrize(
    ("meta", "mpp"),
    [
        ({"PhysicalSizeX": 500, "PhysicalSizeXUnit": "nm", "PhysicalSizeY": 0.5}, (0.5, 0.5)),
        ({"PhysicalSizeX": 2.5e-5, "PhysicalSizeXUnit": "cm", "PhysicalSizeY": 0.25}, (0.25, 0.25)),
        ({"PhysicalSizeXUnit": "pixel"}, None),  # not a length
        ({"PhysicalSizeX": None, "PhysicalSizeY": None}, None),
        ({"PhysicalSizeX": 0.001, "PhysicalSizeY": 0.001}, None),  # implausible
    ],
)
def test_mpp_from_physical_size(
    tmp_path: Path, meta: dict[str, Any], mpp: tuple[float, float] | None
) -> None:
    path = tmp_path / "a.ome.tif"
    write_ome(path, metadata={k: v for k, v in meta.items() if v is not None})
    if meta.get("PhysicalSizeX", 1) is None:
        set_description(
            path,
            lambda x: x.replace(' PhysicalSizeX="0.5"', "").replace(' PhysicalSizeY="0.5"', ""),
        )
    if mpp is None:
        with pytest.raises(SlideMetadataError, match="refusing to guess"):
            open_ome(path)
        assert open_ome(path, mpp_override=(0.3, 0.3)).provenance["mpp_source"] == "mpp_override"
    else:
        assert open_ome(path).mpp == pytest.approx(mpp)


@pytest.mark.parametrize(
    ("data", "meta"),
    [
        (np.zeros((4, 256, 256), np.uint8), {"axes": "CYX"}),  # fluorescence channels
        (np.zeros((2, 256, 256, 3), np.uint8), {"axes": "ZYXS"}),  # a z-stack
        (np.zeros((256, 256, 3), np.uint16), {"axes": "YXS"}),  # 16-bit
    ],
)
def test_other_images_are_refused_for_now(
    tmp_path: Path, data: np.ndarray, meta: dict[str, Any]
) -> None:
    path = tmp_path / "x.ome.tif"
    photometric = "minisblack" if meta["axes"] == "CYX" else "rgb"
    write_ome(path, [data], metadata=meta, photometric=photometric)
    with pytest.raises(SlideMetadataError, match="not an 8-bit RGB image with one plane"):
        open_ome(path, options=ReaderOptions(channels=(0,)))


@pytest.mark.parametrize("kind", ["planar", "grey channels", "legacy"])
def test_unvalidated_layouts_are_refused(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "x.ome.tif"
    if kind == "planar":  # RGB stored as three planes (bfconvert from TIFF or JPEG)
        planes = [np.moveaxis(level, -1, 0).copy() for level in rgb_levels()]
        write_ome(path, planes, metadata={"axes": "SYX"}, planarconfig="separate")
    elif kind == "grey channels":  # raw2ometiff's default output: three grey channels
        planes = [np.moveaxis(level, -1, 0).copy() for level in rgb_levels()]
        write_ome(path, planes, metadata={"axes": "CYX"}, photometric="minisblack")
    else:  # levels as top-level pages that no OME image names (raw2ometiff --legacy)
        write_ome(path, rgb_levels(n=1), extra_pages=rgb_levels(n=3)[1:])
    with pytest.raises(SlideMetadataError, match="interleaved RGB levels in SubIFDs"):
        open_ome(path)


def test_images_in_other_files_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.ome.tif"
    write_ome(path)
    other = '<TiffData IFD="0" PlaneCount="1"><UUID FileName="b.ome.tif">urn:uuid:other</UUID>'
    set_description(
        path,
        lambda x: x.replace('<TiffData IFD="0" PlaneCount="1">', other).replace(
            '<TiffData IFD="0" PlaneCount="1"/>', other + "</TiffData>"
        ),
    )
    with pytest.raises(SlideMetadataError, match="refers to images in other files"):
        open_ome(path)


def test_this_files_own_uuid_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "x.ome.tif"
    write_ome(path)

    def own(xml: str) -> str:
        uuid = xml.split('UUID="')[1].split('"')[0]
        tag = f'<TiffData IFD="0" PlaneCount="1"><UUID FileName="renamed.ome.tif">{uuid}</UUID>'
        return xml.replace('<TiffData IFD="0" PlaneCount="1"/>', tag + "</TiffData>")

    set_description(path, own)
    assert open_ome(path).level_dimensions[0] == (800, 600)


def test_two_images_of_the_same_size_are_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "x.ome.tif"
    write_ome(path, rgb_levels(n=1), extra_images=rgb_levels(n=1))
    with pytest.raises(SlideMetadataError, match="layout SlideTwin validated"):
        open_ome(path)


def test_two_pyramids_are_refused(tmp_path: Path) -> None:
    # two tissue regions, each its own pyramid: serving the larger alone would hide the other
    path = tmp_path / "two.ome.tif"
    kw: dict[str, Any] = {"tile": (256, 256), "photometric": "rgb"}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as tw:
        for i, levels in enumerate((rgb_levels(n=3), rgb_levels(512, 384, n=2, seed=1))):
            meta = {"axes": "YXS", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "Name": f"SECRET{i}"}
            tw.write(levels[0], subifds=len(levels) - 1, metadata=meta, **kw)
            for level in levels[1:]:
                tw.write(level, subfiletype=1, **kw)
    with pytest.raises(SlideMetadataError, match="more than one image pyramid") as err:
        open_ome(path)
    assert "SECRET" not in f"{err.value.message} {err.value.hint}"


def test_no_scene_plane_or_channels(tmp_path: Path) -> None:
    path = tmp_path / "a.ome.tif"
    write_ome(path)
    with pytest.raises(SlideMetadataError, match="has no plane to choose"):
        open_ome(path, options=ReaderOptions(plane=0))


def test_damaged_ome_xml(tmp_path: Path) -> None:
    path = tmp_path / "a.ome.tif"
    write_ome(path)
    set_description(path, lambda x: x.replace("<Image ", "<Image <", 1))
    with pytest.raises(SlideMetadataError, match=r"could not be opened .*\(ParseError\)"):
        open_ome(path)
