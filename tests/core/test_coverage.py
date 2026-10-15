"""Tissue mask, pieces and coverage on hand-made slides and views (SPEC 11 "coverage")."""

from __future__ import annotations

import numpy as np
import pytest

from slidetwin.core.config import ConfigStore, CoverageRules
from slidetwin.core.coverage import (
    build_tissue_map,
    coverage,
    otsu_threshold,
    saturation,
    seen_cells,
    tissue_map,
    tissue_mask,
)
from slidetwin.core.geometry import BBox
from tests.helpers.synth import PINK, PURPLE, line_source, tissue_source


@pytest.fixture(scope="module")
def rules(configs: ConfigStore) -> CoverageRules:
    return configs.protocol("sln_metastasis").coverage  # grid 32 um


def test_otsu_splits_two_modes() -> None:
    values = np.concatenate([np.full(500, 10), np.full(500, 200)]).astype(np.uint8)
    t = otsu_threshold(values)
    assert 10 <= t < 200
    assert otsu_threshold(np.zeros(0, np.uint8)) == 0


def test_saturation() -> None:
    px = np.array([[[255, 255, 255], [255, 0, 0], [128, 128, 128], [0, 0, 0]]], np.uint8)
    assert saturation(px).tolist() == [[0, 255, 0, 0]]


def test_mask_finds_tissue_fills_holes_and_drops_specks() -> None:
    img = np.full((60, 60, 3), 255, np.uint8)
    img[10:30, 10:30] = PINK
    img[18:22, 18:22] = 255  # a lumen inside the tissue
    img[45:48, 45:48] = PURPLE  # a speck of 9 cells
    img[0:12, 50:60] = PURPLE  # tissue touching the image edge
    mask = tissue_mask(img, min_saturation=12, min_cells=20)
    assert mask[11:29, 11:29].all()  # the lumen is filled; corners may round off
    assert not mask[45:48, 45:48].any()
    assert mask[0:11, 51:60].all()  # closing does not eat tissue at the image edge
    assert not mask[40:44, 0:40].any()


def test_blank_slide_has_no_tissue(rules: CoverageRules) -> None:
    tmap = build_tissue_map(line_source(), rules)
    assert not tmap.mask.any()
    assert tmap.pieces == ()
    cov = coverage(tmap, seen_cells(tmap, []), 10)
    assert cov.fraction == 1.0  # nothing to cover
    assert cov.tissue_mm2 == 0.0


def test_pale_tissue_counts(rules: CoverageRules) -> None:
    pale = (238, 214, 226)  # saturation 25: a pale pink like perinodal fat and stroma
    src = tissue_source(rects=((100, 100, 800, 600, pale),), name="pale")
    assert build_tissue_map(src, rules).pieces[0].area_mm2 == pytest.approx(0.48, rel=0.1)


def test_pieces_are_numbered_by_area(rules: CoverageRules) -> None:
    tmap = build_tissue_map(tissue_source(), rules)
    assert [p.piece for p in tmap.pieces] == [1, 2]
    big, small = tmap.pieces
    # 800 x 600 um and 400 x 400 um, snapped to the 32 um grid
    assert big.area_mm2 == pytest.approx(0.48, rel=0.1)
    assert small.area_mm2 == pytest.approx(0.16, rel=0.15)
    assert big.bbox_um.x0 == pytest.approx(96, abs=32)
    assert small.bbox_um.y0 == pytest.approx(896, abs=32)
    assert set(np.unique(tmap.labels)) == {0, 1, 2}


def test_coverage_counts_cells_whose_centre_was_seen(rules: CoverageRules) -> None:
    tmap = build_tissue_map(tissue_source(), rules)
    big = tmap.pieces[0].bbox_um
    left_half = BBox(0.0, 0.0, (big.x0 + big.x1) / 2, 2000.0)
    seen = seen_cells(tmap, [left_half])
    cov = coverage(tmap, seen, 10, tmap.labels == 1)
    assert cov.fraction == pytest.approx(0.5, abs=0.05)
    whole = coverage(tmap, seen, 10)
    assert whole.fraction == pytest.approx(cov.covered_mm2 / whole.tissue_mm2)
    everything = coverage(tmap, seen_cells(tmap, [BBox(0, 0, 4000, 4000)]), 10)
    assert everything.fraction == 1.0
    assert set(everything.to_dict()) == {
        "fraction", "tissue_mm2", "covered_mm2", "at_or_above_mag", "grid_um"
    }  # fmt: skip


def test_cells_in_uses_cell_centres(rules: CoverageRules) -> None:
    tmap = build_tissue_map(tissue_source(), rules)
    # holds the centre of cell (0, 0) at 16 um but not that of cell (1, 1) at 48 um (half-open)
    rs, cs = tmap.cells_in(BBox(16.0, 16.0, 48.0, 48.0))
    assert (rs.start, rs.stop, cs.start, cs.stop) == (0, 1, 0, 1)
    rs, cs = tmap.cells_in(BBox(-100.0, -100.0, -50.0, -50.0))
    assert rs.start == rs.stop == 0


def test_tissue_map_is_shared(rules: CoverageRules) -> None:
    src = tissue_source(name="shared")
    a = tissue_map(src, rules)
    b = tissue_map(src, rules)
    assert a is b
    assert tissue_map(tissue_source(name="other"), rules) is not a
