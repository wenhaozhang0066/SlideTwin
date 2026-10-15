"""Rendering exactness: features land where the micrometre geometry says they should."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slidetwin.core import render
from slidetwin.core.errors import InvalidParameter, SlideMetadataError
from slidetwin.core.geometry import BBox, mpp_for_mag
from slidetwin.core.render import render_region
from slidetwin.core.slide_source import ArraySlideSource, SlideSource
from tests.helpers.synth import dark_centroid, line_source

MPP0 = 0.25
# An 8-px dark band centred on column 1024. Box-averaged pyramid levels move a feature to the centre
# of its block, so only features symmetric about a power-of-two boundary keep their centroid at
# every level; this isolates the renderer's own sub-pixel mapping.
BAND = tuple(range(1020, 1028))
LINE_UM = 1024 * MPP0  # centre of the band in um


def expected_index(x_um: float, x0: float, mpp: float) -> float:
    """Pixel-index position (centres at k) of a feature centred at x_um."""
    return (x_um - x0) / mpp - 0.5


@pytest.fixture(scope="module")
def src40() -> SlideSource:
    return line_source(mpp=MPP0, n_levels=5, xs=BAND, ys=(), name="s40")


@pytest.fixture(scope="module")
def band() -> tuple[SlideSource, SlideSource]:
    """A 33-px dark band (8.25 um) with and without a pyramid, for low magnifications."""
    xs = tuple(range(1000, 1033))
    return (
        line_source(mpp=MPP0, n_levels=6, xs=xs, ys=()),
        line_source(mpp=MPP0, n_levels=1, xs=xs, ys=()),
    )


@pytest.mark.parametrize("mag", [5, 10, 20, 40])
@pytest.mark.parametrize("offset", [0.0, 0.1, 0.37, 0.5, 0.83])
def test_line_lands_at_expected_subpixel(src40: SlideSource, mag: float, offset: float) -> None:
    mpp = mpp_for_mag(mag)
    x0 = LINE_UM - 32 * mpp + offset * mpp
    bbox = BBox(x0, 100.0, x0 + 64 * mpp, 100.0 + 8 * mpp)
    img = render_region(src40, bbox, (64, 8))
    measured = dark_centroid(img[4, :, 0])
    assert measured == pytest.approx(expected_index(LINE_UM, x0, mpp), abs=0.15)


@settings(max_examples=40, deadline=None)
@given(st.floats(0.0, 1.0), st.sampled_from([5.0, 10.0, 20.0, 40.0]), st.integers(16, 120))
def test_line_position_property(offset: float, mag: float, width: int) -> None:
    src = line_source(mpp=MPP0, n_levels=5, xs=BAND, ys=(), name="p")
    mpp = mpp_for_mag(mag)
    x0 = LINE_UM - (width / 2) * mpp + offset * mpp
    img = render_region(src, BBox(x0, 50.0, x0 + width * mpp, 50.0 + 4 * mpp), (width, 4))
    assert dark_centroid(img[2, :, 0]) == pytest.approx(expected_index(LINE_UM, x0, mpp), abs=0.15)


@pytest.mark.parametrize("mag", [1.25, 2.5])
def test_band_position_at_low_power_with_and_without_pyramid(
    band: tuple[SlideSource, SlideSource], mag: float
) -> None:
    centre_um = (1000 + 1033) / 2 * MPP0
    mpp = mpp_for_mag(mag)
    x0 = centre_um - 20 * mpp + 0.3 * mpp
    bbox = BBox(x0, 40.0, x0 + 40 * mpp, 40.0 + 4 * mpp)
    with_pyr = render_region(band[0], bbox, (40, 4))
    single = render_region(band[1], bbox, (40, 4))  # integer pre-reduction path
    exp = expected_index(centre_um, x0, mpp)
    assert dark_centroid(with_pyr[2, :, 0]) == pytest.approx(exp, abs=0.15)
    assert dark_centroid(single[2, :, 0]) == pytest.approx(exp, abs=0.15)
    assert np.abs(with_pyr.astype(int) - single.astype(int)).mean() < 2.0


def test_native_resolution_is_a_copy(src40: SlideSource) -> None:
    bbox = BBox(100.0, 100.0, 100.0 + 64 * MPP0, 100.0 + 48 * MPP0)  # integer-aligned at level 0
    img = render_region(src40, bbox, (64, 48))
    ref = src40.read_level(0, 400, 400, 64, 48)
    assert np.abs(img.astype(int) - ref.astype(int)).max() <= 1


def test_outside_slide_is_fill(src40: SlideSource) -> None:
    # slide is 512 x 384 um; the left half of this box is outside
    bbox = BBox(-256.0, 0.0, 256.0, 384.0)
    img = render_region(src40, bbox, (512, 384), fill_rgb=(10, 20, 30))
    assert (img[:, :250] == (10, 20, 30)).all()
    assert (img[:, 262:] != (10, 20, 30)).any()


def test_upsampling_only_within_tolerance_keeps_geometry() -> None:
    # a nominal-40x scan at 0.2632 um/px rendered at 0.25 um/px (inside the 6% tolerance)
    src = line_source(width=512, height=256, mpp=0.2632, n_levels=1, xs=(300,), ys=(), name="gt450")
    centre = 300.5 * 0.2632
    x0 = centre - 20 * 0.25 + 0.2 * 0.25
    img = render_region(src, BBox(x0, 10.0, x0 + 40 * 0.25, 10.0 + 4 * 0.25), (40, 4))
    assert dark_centroid(img[2, :, 0]) == pytest.approx(expected_index(centre, x0, 0.25), abs=0.15)


def test_render_is_deterministic(src40: SlideSource) -> None:
    bbox = BBox(33.3, 44.4, 33.3 + 300.0, 44.4 + 200.0)
    a = render_region(src40, bbox, (300, 200))
    b = render_region(src40, bbox, (300, 200))
    np.testing.assert_array_equal(a, b)


def test_render_rejects_bad_requests(src40: SlideSource) -> None:
    with pytest.raises(InvalidParameter):
        render_region(src40, BBox(0.0, 0.0, 10.0, 10.0), (0, 10))
    with pytest.raises(InvalidParameter):
        render_region(src40, BBox(0.0, 0.0, 0.0, 10.0), (10, 10))


def test_shallow_pyramid_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    src = ArraySlideSource([np.full((512, 512, 3), 255, np.uint8)], (0.25, 0.25))
    monkeypatch.setattr(render, "MAX_READ_PIXELS", 1000)
    with pytest.raises(SlideMetadataError):
        render_region(src, BBox(0.0, 0.0, 128.0, 128.0), (16, 16))


def test_region_entirely_outside_is_all_fill(src40: SlideSource) -> None:
    img = render_region(src40, BBox(-900.0, -900.0, -500.0, -600.0), (40, 30), fill_rgb=(1, 2, 3))
    assert (img == (1, 2, 3)).all()
