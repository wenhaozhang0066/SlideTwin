"""Geometry and coordinate tests (written before slidetwin/core/geometry.py)."""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from slidetwin.core.errors import InvalidParameter
from slidetwin.core.geometry import (
    BBox,
    LevelTransform,
    available_mags,
    choose_level,
    clamp_center,
    crop_geometry,
    displacement_in_viewports,
    distance,
    extent_transforms,
    format_mag,
    fov_um,
    integer_transforms,
    is_mag_available,
    level_mpps,
    mag_for_mpp,
    mpp_for_mag,
    thumbnail_size,
    transform_mpps,
    um_to_view_px,
    view_px_to_um,
    viewport_bbox,
)

PRESETS = (1.25, 2.5, 5.0, 10.0, 20.0, 40.0)
finite = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
positive_mpp = st.floats(min_value=0.05, max_value=20.0, allow_nan=False)
viewport = st.tuples(st.integers(16, 4096), st.integers(16, 4096))


# --- magnification convention -------------------------------------------------------------


@pytest.mark.parametrize(
    ("mag", "mpp"), [(40, 0.25), (20, 0.5), (10, 1.0), (5, 2.0), (2.5, 4.0), (1.25, 8.0)]
)
def test_mpp_for_mag_table(mag: float, mpp: float) -> None:
    assert mpp_for_mag(mag) == mpp


def test_mpp_ref_is_configurable() -> None:
    assert mpp_for_mag(40, mpp_ref_40x=0.2) == pytest.approx(0.2)
    assert mpp_for_mag(20, mpp_ref_40x=0.2) == pytest.approx(0.4)


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
def test_mpp_for_mag_rejects_bad_mag(bad: float) -> None:
    with pytest.raises(InvalidParameter):
        mpp_for_mag(bad)
    with pytest.raises(InvalidParameter):
        mag_for_mpp(bad)


def test_mpp_for_mag_rejects_bad_reference() -> None:
    with pytest.raises(InvalidParameter):
        mpp_for_mag(10, mpp_ref_40x=0.0)


@given(st.floats(min_value=0.01, max_value=400.0))
def test_mag_mpp_round_trip(mag: float) -> None:
    assert math.isclose(mag_for_mpp(mpp_for_mag(mag)), mag, rel_tol=1e-12)


def test_format_mag() -> None:
    assert format_mag(1.25) == "1.25x"
    assert format_mag(10.0) == "10x"
    assert format_mag(40) == "40x"


# --- availability (never fake a magnification) -------------------------------------------


def test_cmu_like_slide_has_no_40x() -> None:
    assert not is_mag_available(40, 0.499, tolerance=0.06)
    assert is_mag_available(20, 0.499, tolerance=0.06)
    assert available_mags(PRESETS, 0.499, tolerance=0.06) == (1.25, 2.5, 5.0, 10.0, 20.0)


def test_tolerance_admits_nominal_40x_scanners() -> None:
    assert is_mag_available(40, 0.2632, tolerance=0.06)  # 0.25 >= 0.2474
    assert not is_mag_available(40, 0.27, tolerance=0.06)  # 0.25 < 0.2538
    assert not is_mag_available(40, 0.2632, tolerance=0.0)
    assert is_mag_available(40, 0.25, tolerance=0.0)


def test_availability_rejects_bad_inputs() -> None:
    with pytest.raises(InvalidParameter):
        is_mag_available(40, 0.0)
    with pytest.raises(InvalidParameter):
        is_mag_available(40, 0.25, tolerance=1.0)
    with pytest.raises(InvalidParameter):
        is_mag_available(40, 0.25, tolerance=-0.1)


@given(positive_mpp, st.floats(min_value=0.0, max_value=0.5))
def test_available_mags_is_exact_partition(native: float, tol: float) -> None:
    avail = available_mags(PRESETS, native, tolerance=tol)
    assert list(avail) == sorted(avail)
    for m in PRESETS:
        expected = mpp_for_mag(m) >= native * (1 - tol)
        assert (m in avail) == expected


# --- pyramid level choice -------------------------------------------------------------------

PYRAMID = [(0.25 * 2**k, 0.25 * 2**k) for k in range(7)]  # 0.25 .. 16 um/px


@pytest.mark.parametrize(
    ("target", "level"),
    [(1.0, 2), (1.9, 2), (2.0, 3), (0.25, 0), (0.2, 0), (100.0, 6), (0.26, 0), (16.0, 6)],
)
def test_choose_level_examples(target: float, level: int) -> None:
    assert choose_level(PYRAMID, (target, target)) == level


def test_choose_level_tolerates_float_noise() -> None:
    assert choose_level(PYRAMID, (1.0 * (1 - 1e-12), 1.0)) == 2


def test_choose_level_uses_both_axes() -> None:
    levels = [(0.25, 0.25), (0.5, 0.52)]
    assert choose_level(levels, (0.5, 0.5)) == 0  # level 1 too coarse on y
    assert choose_level(levels, (0.5, 0.52)) == 1


def test_choose_level_rejects_empty() -> None:
    with pytest.raises(InvalidParameter):
        choose_level([], (1.0, 1.0))


@given(
    st.lists(st.floats(min_value=0.1, max_value=100.0), min_size=1, max_size=10),
    st.floats(min_value=0.05, max_value=200.0),
)
def test_choose_level_never_upsamples_when_avoidable(mpps: list[float], target: float) -> None:
    levels = [(m, m) for m in mpps]
    chosen = choose_level(levels, (target, target))
    qualifying = [i for i, m in enumerate(mpps) if m <= target * (1 + 1e-9)]
    if qualifying:
        assert chosen in qualifying
        assert mpps[chosen] == max(mpps[i] for i in qualifying)
    else:
        assert mpps[chosen] == min(mpps)


def test_level_mpps_per_axis() -> None:
    out = level_mpps((1000, 600), [(1000, 600), (500, 300), (250, 151)], (0.25, 0.3))
    assert out[0] == (0.25, 0.3)
    assert out[1] == (0.5, 0.6)
    assert out[2][0] == pytest.approx(1.0)
    assert out[2][1] == pytest.approx(0.3 * 600 / 151)


# --- viewport geometry -----------------------------------------------------------------------


def test_viewport_bbox_matches_appendix_b() -> None:
    # SPEC appendix B: 10x (1.0 um/px), 1024x768 viewport, centre (18350, 9120)
    bbox = viewport_bbox((18350.0, 9120.0), (1024, 768), 1.0)
    assert bbox.to_list() == [17838.0, 8736.0, 18862.0, 9504.0]
    assert fov_um((1024, 768), 1.0) == (1024.0, 768.0)


@given(st.tuples(finite, finite), viewport, positive_mpp)
def test_viewport_bbox_properties(
    center: tuple[float, float], vp: tuple[int, int], mpp: float
) -> None:
    bbox = viewport_bbox(center, vp, mpp)
    assert math.isclose(bbox.width, vp[0] * mpp, rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(bbox.height, vp[1] * mpp, rel_tol=1e-9, abs_tol=1e-6)
    cx, cy = bbox.center
    assert math.isclose(cx, center[0], rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(cy, center[1], rel_tol=1e-9, abs_tol=1e-6)


def test_viewport_rejects_bad_size() -> None:
    with pytest.raises(InvalidParameter):
        viewport_bbox((0.0, 0.0), (0, 768), 1.0)
    with pytest.raises(InvalidParameter):
        fov_um((1024, 768), -1.0)


def test_clamp_examples() -> None:
    slide = (10000.0, 5000.0)
    fov = (1024.0, 768.0)
    assert clamp_center((0.0, 0.0), fov, slide) == (512.0, 384.0)
    assert clamp_center((20000.0, 20000.0), fov, slide) == (9488.0, 4616.0)
    assert clamp_center((3000.0, 2000.0), fov, slide) == (3000.0, 2000.0)
    # field of view wider than the slide on x: centred on x, clamped on y
    assert clamp_center((0.0, 0.0), (12000.0, 768.0), slide) == (5000.0, 384.0)


@given(
    st.tuples(finite, finite),
    st.tuples(st.floats(1.0, 5e4), st.floats(1.0, 5e4)),
    st.tuples(st.floats(1.0, 1e5), st.floats(1.0, 1e5)),
)
def test_clamp_properties(
    center: tuple[float, float], fov: tuple[float, float], slide: tuple[float, float]
) -> None:
    c = clamp_center(center, fov, slide)
    assert clamp_center(c, fov, slide) == c  # idempotent
    for axis in (0, 1):
        if fov[axis] >= slide[axis]:
            assert c[axis] == slide[axis] / 2
        else:
            lo, hi = c[axis] - fov[axis] / 2, c[axis] + fov[axis] / 2
            assert lo >= -1e-9 * slide[axis]
            assert hi <= slide[axis] * (1 + 1e-9)


@given(
    st.tuples(finite, finite),
    viewport,
    positive_mpp,
    st.tuples(st.floats(0, 4096), st.floats(0, 4096)),
)
def test_view_px_um_round_trip(
    center: tuple[float, float], vp: tuple[int, int], mpp: float, p: tuple[float, float]
) -> None:
    bbox = viewport_bbox(center, vp, mpp)
    um = view_px_to_um(p, bbox, mpp)
    back = um_to_view_px(um, bbox, mpp)
    assert math.isclose(back[0], p[0], rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(back[1], p[1], rel_tol=1e-9, abs_tol=1e-6)


def test_view_px_to_um_example() -> None:
    bbox = viewport_bbox((18350.0, 9120.0), (1024, 768), 1.0)
    assert view_px_to_um((0.0, 0.0), bbox, 1.0) == (17838.0, 8736.0)
    assert view_px_to_um((512.0, 384.0), bbox, 1.0) == (18350.0, 9120.0)


def test_distance_and_displacement() -> None:
    assert distance((0.0, 0.0), (3.0, 4.0)) == 5.0
    fov = (1024.0, 768.0)
    assert displacement_in_viewports((0.0, 0.0), (512.0, 0.0), fov) == 0.5
    assert displacement_in_viewports((0.0, 0.0), (1024.0, 768.0), fov) == pytest.approx(
        math.sqrt(2)
    )
    assert displacement_in_viewports((5.0, 5.0), (5.0, 5.0), fov) == 0.0


# --- thumbnails and crops ---------------------------------------------------------------------


def test_thumbnail_size() -> None:
    assert thumbnail_size((2000.0, 1000.0), 1024, 0.25) == (1024, 512)
    assert thumbnail_size((1000.0, 2000.0), 1024, 0.25) == (512, 1024)
    assert thumbnail_size((100000.0, 10.0), 1024, 0.25) == (1024, 1)
    with pytest.raises(InvalidParameter):
        thumbnail_size((0.0, 10.0), 1024, 0.25)


def test_thumbnail_is_never_finer_than_the_scan() -> None:
    # a slide only 500 x 250 px at 1 um/px: the thumbnail stays at the scan resolution
    assert thumbnail_size((500.0, 250.0), 1024, 1.0) == (500, 250)
    # rounding must not push one axis below the native pixel size
    w, h = thumbnail_size((500.4, 250.6), 1024, 1.0)
    assert 500.4 / w >= 1.0
    assert 250.6 / h >= 1.0


@given(
    st.floats(1.0, 1e5),
    st.floats(1.0, 1e5),
    st.integers(16, 4096),
    st.floats(0.05, 20.0),
)
def test_thumbnail_properties(w: float, h: float, long_side: int, native: float) -> None:
    tw, th = thumbnail_size((w, h), long_side, native)
    assert 1 <= max(tw, th) <= long_side
    assert min(tw, th) >= 1
    for extent, n in ((w, tw), (h, th)):
        assert extent / n >= native * (1 - 1e-12) or n == 1


def test_crop_scales_to_viewport_long_side() -> None:
    bbox, size, mpp = crop_geometry(
        BBox(100.0, 200.0, 600.0, 450.0), (10000.0, 10000.0), 1024, 0.25
    )
    assert size == (1024, 512)
    assert mpp == pytest.approx(500 / 1024)
    assert bbox.x0 == 100.0
    assert bbox.y0 == 200.0
    assert bbox.width == pytest.approx(size[0] * mpp)
    assert bbox.height == pytest.approx(size[1] * mpp)


def test_crop_never_upsamples() -> None:
    _, size, mpp = crop_geometry(BBox(0.0, 0.0, 50.0, 50.0), (10000.0, 10000.0), 1024, 0.25)
    assert mpp == 0.25
    assert size == (200, 200)


def test_crop_is_clamped_to_slide() -> None:
    bbox, _, _ = crop_geometry(BBox(-100.0, -100.0, 400.0, 400.0), (1000.0, 1000.0), 1024, 0.25)
    assert bbox.x0 == 0.0
    assert bbox.y0 == 0.0
    assert bbox.x1 <= 1000.0
    with pytest.raises(InvalidParameter):
        crop_geometry(BBox(2000.0, 2000.0, 2100.0, 2100.0), (1000.0, 1000.0), 1024, 0.25)
    with pytest.raises(InvalidParameter):
        crop_geometry(BBox(10.0, 10.0, 10.0, 50.0), (1000.0, 1000.0), 1024, 0.25)


@given(
    st.floats(0, 9000),
    st.floats(0, 9000),
    st.floats(1, 5000),
    st.floats(1, 5000),
    st.integers(64, 4096),
    st.floats(0.1, 1.0),
)
def test_crop_properties(
    x: float, y: float, w: float, h: float, long_side: int, native: float
) -> None:
    req = BBox(x, y, x + w, y + h)
    slide = (10000.0, 10000.0)
    clipped = req.intersection(BBox(0.0, 0.0, *slide))
    assume(clipped is not None and clipped.width > 0 and clipped.height > 0)
    bbox, size, mpp = crop_geometry(req, slide, long_side, native)
    assert mpp >= native  # never finer than the scan
    assert max(size) <= long_side
    assert min(size) >= 1
    assert math.isclose(bbox.width, size[0] * mpp, rel_tol=1e-9)
    assert math.isclose(bbox.height, size[1] * mpp, rel_tol=1e-9)
    # rounding moves an edge by at most half a pixel; a side thinner than half a pixel becomes 1 px
    for got, want in ((bbox.width, clipped.width), (bbox.height, clipped.height)):
        assert abs(got - want) <= max(mpp / 2, mpp - want) + 1e-9 * want
    assert bbox.x0 == clipped.x0
    assert bbox.y0 == clipped.y0


# --- BBox ------------------------------------------------------------------------------------


def test_bbox_basics() -> None:
    b = BBox(0.0, 0.0, 10.0, 20.0)
    assert (b.width, b.height, b.area, b.center) == (10.0, 20.0, 200.0, (5.0, 10.0))
    assert b.contains(0.0, 0.0)
    assert b.contains(10.0, 20.0)
    assert not b.contains(10.1, 5.0)
    assert b.intersection(BBox(5.0, 5.0, 15.0, 15.0)) == BBox(5.0, 5.0, 10.0, 15.0)
    assert b.intersection(BBox(20.0, 20.0, 30.0, 30.0)) is None
    assert b.intersection(BBox(10.0, 0.0, 30.0, 30.0)) is None  # touching edge only
    with pytest.raises(InvalidParameter):
        BBox(0.0, 0.0, -1.0, 5.0)
    with pytest.raises(InvalidParameter):
        BBox(0.0, math.nan, 1.0, 5.0)


# --- level transforms -----------------------------------------------------------------------


def test_extent_transforms_are_d13() -> None:
    dims = [(1000, 800), (500, 400), (126, 99)]
    transforms = extent_transforms(dims[0], dims)
    assert transforms[0] == LevelTransform(1.0, 1.0)
    assert transforms[1] == LevelTransform(2.0, 2.0)
    assert transforms[2] == LevelTransform(1000 / 126, 800 / 99)
    assert transform_mpps(transforms, (0.5, 0.25)) == level_mpps(dims[0], dims, (0.5, 0.25))


def test_integer_transforms_take_the_exact_scale() -> None:
    dims = [
        (46000, 32914),
        (11500, 8228),  # floor of / 4 (SVS); the extent ratio of y would be 4.0002
        (2875, 2058),  # ceil of / 16 in y (libvips, QuPath)
        (23000, 16457),  # exactly / 2
        (13800, 9874),  # 46000 / 3.33: no integer k
        (11500, 6000),  # x is / 4 but y is not
    ]
    t = integer_transforms(dims)
    assert t[:4] == tuple(LevelTransform(k, k) for k in (1.0, 4.0, 16.0, 2.0))
    assert t[4:] == extent_transforms(dims[0], dims[4:])


@pytest.mark.parametrize(
    "values",
    [
        (0.0, 1.0),
        (1.0, -2.0),
        (math.nan, 1.0),
        (1.0, 1.0, math.inf, 0.0),
        (1.0, 1.0, 0.0, math.nan),
    ],
)
def test_invalid_level_transforms_are_refused(values: tuple[float, ...]) -> None:
    with pytest.raises(InvalidParameter):
        LevelTransform(*values)
