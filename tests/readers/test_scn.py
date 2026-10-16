"""Leica SCN reader (M8): regions as scenes, the macro never read, brightfield only, XML mpp."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.slide_source import ReaderOptions, source_provenance
from slidetwin.readers import open_source
from slidetwin.readers.scn import open_reader
from slidetwin.readers.tiffpages import TiffPagesSource
from tests.conftest import sample_path
from tests.helpers.make_scn import NS, Roi, write_scn

MATCH = FormatMatch("leica-scn", "scn")
EXPERIMENTAL = ReaderOptions(experimental=True)


def open_scn(path: Path, **kw: Any) -> TiffPagesSource:
    kw.setdefault("mpp_override", None)
    options = kw.pop("options", ReaderOptions())
    return open_reader(path, MATCH, name="s1", options=options, **kw)


def full(src: TiffPagesSource, level: int) -> np.ndarray:
    w, h = src.level_dimensions[level]
    return src.read_level(level, 0, 0, w, h)


def refused(path: Path, match: str, **kw: Any) -> SlideMetadataError:
    with pytest.raises(SlideMetadataError, match=match) as err:
        open_scn(path, **kw)
    assert "SECRET" not in f"{err.value.message} {err.value.hint}"
    return err.value


def test_one_brightfield_region(tmp_path: Path) -> None:
    f = write_scn(tmp_path / "a.scn", [Roi()])
    assert sniff(f.path) == MATCH
    src = open_source(f.path, sniff(f.path), name="s1")
    assert isinstance(src, TiffPagesSource)
    assert src.level_dimensions == ((1024, 768), (256, 192), (64, 48))  # the macro is not a level
    assert src.level_transforms[1] == LevelTransform(4.0, 4.0)  # XML nm/px = extent ratio
    assert src.mpp == pytest.approx((0.5, 0.5))
    for k in range(3):
        np.testing.assert_array_equal(full(src, k), f.truth[0][0][k])
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"]) == ("leica-scn", "scn view nm / level-0 px")
    assert not {"scene", "plane", "experimental"} & set(prov)
    assert "SECRET" not in json.dumps(prov)
    src.close()


def test_level0_matches_openslide(tmp_path: Path) -> None:
    openslide = pytest.importorskip("openslide")
    f = write_scn(tmp_path / "a.scn", [Roi(offset_nm=(2_000_100, 7_000_300))])
    ref = openslide.OpenSlide(str(f.path))
    x0, y0 = (int(ref.properties[f"openslide.region[0].{k}"]) for k in ("x", "y"))
    expected = np.asarray(ref.read_region((x0, y0), 0, (1024, 768)))[:, :, :3]
    np.testing.assert_array_equal(full(open_scn(f.path), 0), expected)


def test_several_regions_need_a_scene_and_experimental(tmp_path: Path) -> None:
    rois = [Roi(), Roi(w=768, h=512, offset_nm=(2_500_000, 12_000_000), seed=1)]
    f = write_scn(tmp_path / "two.scn", rois)
    refused(f.path, r"has 2 scenes; choose one with scene: 0 to 1\.")
    refused(
        f.path,
        "several regions, which SlideTwin validated only on synthetic samples",
        options=ReaderOptions(scene=1),
    )
    refused(f.path, "has no scene 2", options=ReaderOptions(scene=2, experimental=True))
    src = open_scn(f.path, options=ReaderOptions(scene=1, experimental=True))
    assert src.level_dimensions[0] == (768, 512)
    np.testing.assert_array_equal(full(src, 0), f.truth[1][0][0])
    prov = src.provenance
    assert {k: prov[k] for k in ("scene", "scenes", "experimental")} == {
        "scene": 1,
        "scenes": 2,
        "experimental": True,
    }


def test_mixed_resolution_regions_keep_their_own_mpp(tmp_path: Path) -> None:
    rois = [Roi(), Roi(nm_per_px=250, offset_nm=(6_000_000, 18_000_000), seed=2)]
    path = write_scn(tmp_path / "mixed.scn", rois).path
    first = open_scn(path, options=ReaderOptions(scene=0, experimental=True))
    second = open_scn(path, options=ReaderOptions(scene=1, experimental=True))
    assert first.mpp == pytest.approx((0.5, 0.5))
    assert second.mpp == pytest.approx((0.25, 0.25))


def test_z_stack_needs_a_plane_and_experimental(tmp_path: Path) -> None:
    f = write_scn(tmp_path / "z.scn", [Roi(planes=2)])
    refused(f.path, r"has 2 planes; choose one with plane: 0 to 1\.", options=EXPERIMENTAL)
    refused(f.path, "a z-stack, which SlideTwin validated", options=ReaderOptions(plane=1))
    src = open_scn(f.path, options=ReaderOptions(plane=1, experimental=True))
    for k in range(3):
        np.testing.assert_array_equal(full(src, k), f.truth[0][1][k])
    assert {k: src.provenance[k] for k in ("plane", "planes", "experimental")} == {
        "plane": 1,
        "planes": 2,
        "experimental": True,
    }
    assert "scene" not in src.provenance


def test_fluorescence_is_refused_for_now(tmp_path: Path) -> None:
    path = write_scn(tmp_path / "fl.scn", [Roi()], fluorescence=True).path
    assert sniff(path) == MATCH
    for options in (ReaderOptions(), ReaderOptions(channels=(0, 1))):
        err = refused(path, "fluorescence scan", options=options)
        assert "channel renderer" in (err.hint or "")


def test_options_a_one_region_file_can_take(tmp_path: Path) -> None:
    path = write_scn(tmp_path / "a.scn", [Roi()]).path
    assert "scene" not in open_scn(path, options=ReaderOptions(scene=0, plane=0)).provenance
    refused(path, "has no channels to choose", options=ReaderOptions(channels=(0,)))


def test_unvalidated_xml_versions_are_refused(tmp_path: Path) -> None:
    old = write_scn(tmp_path / "old.scn", [Roi()], namespace=NS.replace("10/01", "03/10")).path
    refused(old, "version or structure SlideTwin has not validated")
    two = write_scn(
        tmp_path / "two.scn",
        [Roi()],
        edit_xml=lambda x: x.replace(
            "</collection>", '</collection><collection sizeX="1" sizeY="1"/>'
        ),
    ).path
    refused(two, "version or structure")


def _swap_levels(xml: str) -> str:
    return (
        xml.replace('r="0" ifd="2"', 'r="X" ifd="2"')
        .replace('r="1" ifd="3"', 'r="0" ifd="3"')
        .replace('r="X" ifd="2"', 'r="1" ifd="2"')
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda x: x.replace('ifd="2"', 'ifd="99"'),  # an IFD the file does not have
        lambda x: x.replace('r="1" ifd="3"', 'r="0" ifd="3"'),  # two IFDs for one level
        lambda x: x.replace('sizeX="256" sizeY="192"', 'sizeX="250" sizeY="192"'),  # wrong size
        lambda x: x.replace('r="2" ifd="4"', 'r="3" ifd="4"'),  # a level missing
        lambda x: x.replace('r="1" ifd="3"', 'r="1" c="1" ifd="3"'),  # channels in brightfield
        lambda x: x.replace(
            "brightfield</illuminationSource></illuminationSettings>"
            "</scanSettings></image></collection>",
            "</illuminationSource></illuminationSettings></scanSettings></image></collection>",
        ),  # no illumination for the region
        _swap_levels,  # levels that grow
    ],
)
def test_inconsistent_xml_is_refused(tmp_path: Path, edit: Callable[[str], str]) -> None:
    path = write_scn(tmp_path / "x.scn", [Roi()], edit_xml=edit).path
    refused(path, "layout SlideTwin validated")


def test_regions_without_view_or_pixels_and_macro_only_files(tmp_path: Path) -> None:
    no_view = write_scn(
        tmp_path / "v.scn",
        [Roi()],
        edit_xml=lambda x: x.replace('<view sizeX="512000"', '<other sizeX="512000"'),
    ).path
    refused(no_view, "layout SlideTwin validated")
    macro_only = write_scn(tmp_path / "m.scn", []).path
    refused(macro_only, "layout SlideTwin validated")


def test_mpp_sources_must_agree_and_be_plausible(tmp_path: Path) -> None:
    disagree = write_scn(tmp_path / "d.scn", [Roi()], tag_scale=2.0).path  # tags say 1 um/px
    refused(disagree, "refusing to guess")
    src = open_scn(disagree, mpp_override=(0.5, 0.5))
    assert src.provenance["mpp_source"] == "mpp_override"
    implausible = write_scn(tmp_path / "i.scn", [Roi(nm_per_px=30_000)]).path  # 30 um/px
    refused(implausible, "refusing to guess")


def test_damaged_xml(tmp_path: Path) -> None:
    path = write_scn(
        tmp_path / "x.scn", [Roi()], edit_xml=lambda x: x.replace("<image ", "<image <", 1)
    ).path
    err = refused(path, "could not be opened")
    assert "ParseError" in err.message


def test_manifest_scene_and_experimental_reach_the_reader(tmp_path: Path) -> None:
    rois = [Roi(), Roi(w=768, h=512, offset_nm=(2_500_000, 12_000_000), seed=1)]
    write_scn(tmp_path / "two.scn", rois)
    entry = {"id": "scn_two", "path": "two.scn", "scene": 1, "experimental": True}
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump({"slides": [entry]}))
    registry = SlideRegistry.from_manifest(tmp_path / "manifest.yaml", tmp_path)
    source = registry.get("scn_two").source
    assert source.level_dimensions[0] == (768, 512)
    assert source_provenance(source)["scene"] == 1
    registry.close()


# --- real samples (tier 3, local) ------------------------------------------------------------


@pytest.mark.testdata
def test_leica_fluorescence_1_is_refused_without_names() -> None:
    path = sample_path("Leica/Leica-Fluorescence-1.scn", tier=3)
    assert sniff(path) == MATCH
    err = refused(path, "fluorescence scan")
    assert "CD31" not in err.message  # the collection is named after the case


@pytest.mark.testdata
def test_leica_1_matches_openslide_at_level0() -> None:
    openslide = pytest.importorskip("openslide")
    path = sample_path("Leica/Leica-1.scn", tier=3)
    src, ref = open_scn(path), openslide.OpenSlide(str(path))
    assert src.level_dimensions == (
        (36832, 38432),
        (9208, 9608),
        (2302, 2402),
        (576, 600),
        (144, 150),
    )
    assert src.mpp == pytest.approx((0.5, 0.5))
    x0, y0 = (int(ref.properties[f"openslide.region[0].{k}"]) for k in ("x", "y"))
    for x, y in ((0, 0), (18000, 20000), (36832 - 512, 38432 - 512), (5000, 31000)):
        expected = np.asarray(ref.read_region((x0 + x, y0 + y), 0, (512, 512)))[:, :, :3]
        np.testing.assert_array_equal(src.read_level(0, x, y, 512, 512), expected)
