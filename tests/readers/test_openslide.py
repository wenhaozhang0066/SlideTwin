"""OpenSlide reader (M8): NDPI, VMS/VMU, MRXS and SVSlide.

Exact integer levels read in their own grid, trusted mpp, file sets, reopening after OpenSlide's
latching errors, and refusals outside the validated cases. Pixels are checked against independent
decoders: Pillow for the JPEGs of NDPI and VMS (including its DCT-scaled "draft" decodes), the
survey's MRXS parser for level 0, and the written source for VMU and SVSlide.
"""

from __future__ import annotations

import hashlib
import io
import shutil
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import openslide
import pytest
import tifffile
import yaml
from PIL import Image

from slidetwin.core.errors import (
    SlideIntegrityError,
    SlideMetadataError,
    SlideNotFound,
    SlideReadError,
)
from slidetwin.core.fileset import fileset_sha256
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import BBox, LevelTransform
from slidetwin.core.registry import SlideRegistry, file_sha256
from slidetwin.core.render import render_region
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers import openslide as reader
from slidetwin.readers.openslide import OpenSlideFamilySource, exact_levels, open_reader
from tests.conftest import sample_path
from tests.helpers.mrxs_oracle import Level0Mosaic
from tests.helpers.openslide_fixtures import (
    LONG,
    SHORT,
    vms_image,
    write_ndpi,
    write_svslide,
    write_vms,
    write_vmu,
)
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff

MRXS = "Mirax/CMU-1-Saved-1_16/CMU-1-Saved-1_16.mrxs"
STEM = "CMU-1-Saved-1_16"


def open_os(path: Path, fmt: str | None = None, **kw: Any) -> OpenSlideFamilySource:
    override = kw.pop("mpp_override", None)
    match = FormatMatch(fmt, "openslide") if fmt else sniff(path)
    return open_reader(path, match, name="s1", mpp_override=override, options=ReaderOptions(**kw))


def decode(data: bytes, scale: int = 1) -> np.ndarray:
    """Pillow's decode; scale 2, 4 or 8 decodes with libjpeg's DCT scaling, as OpenSlide does."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (im.width // scale, im.height // scale))
    return np.asarray(im.convert("RGB"))


def assert_close(a: np.ndarray, b: np.ndarray, mean: float = 0.1, peak: int = 2) -> None:
    """Independent decoders may differ by rounding; they agree exactly on this cluster."""
    assert a.shape == b.shape
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    assert d.mean() <= mean
    assert d.max() <= peak


def copy_mrxs(tmp_path: Path) -> Path:
    src = sample_path(MRXS)
    dest = tmp_path / "mrxs"
    shutil.copytree(src.parent, dest, ignore=shutil.ignore_patterns(".archive-sha256"))
    return dest / f"{STEM}.mrxs"


def edit_slidedat(mrxs: Path, old: str, new: str) -> None:
    ini = mrxs.with_suffix("") / "Slidedat.ini"
    text = ini.read_bytes().decode("utf-8-sig")
    assert old in text
    ini.write_bytes(b"\xef\xbb\xbf" + text.replace(old, new).encode("utf-8"))


# --- 3DHistech MRXS (the tier-1 sample, CC0) ----------------------------------------------------


@pytest.mark.testdata
def test_mrxs_levels_mpp_and_file_set() -> None:
    path = sample_path(MRXS)
    src = open_os(path)
    assert src.level_dimensions == (
        (7436, 15494),
        (3718, 7747),
        (1859, 3873),
        (929, 1936),
        (464, 968),
        (232, 484),
    )
    # MRXS levels are exactly 2^k with sizes rounded down: 7436 / 232 is 32.05, not 32
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(6))
    assert src.mpp == (3.7172362555720699, 3.7163168316831698)
    prov = src.provenance
    assert prov["reader"] == "openslide"
    assert (prov["format"], prov["mpp_source"]) == ("mirax", "mirax MICROMETER_PER_PIXEL_X/Y")
    assert (prov["fill"], prov["downsamples"]) == ([255, 255, 255], [1, 2, 4, 8, 16, 32])
    assert set(prov["versions"]) == {"openslide", "openslide-python"}
    assert src.fileset is not None
    root, files = src.fileset
    assert root == path.parent
    assert len(files) == 22  # the .mrxs, Slidedat.ini, the index and 19 data files
    assert files[0] == f"{STEM}.mrxs"
    assert {f"{STEM}/Slidedat.ini", f"{STEM}/Index.dat", f"{STEM}/Data0018.dat"} <= set(files)
    src.close()


@pytest.mark.testdata
def test_mrxs_level0_matches_the_independent_parser() -> None:
    path = sample_path(MRXS)
    src, oracle = open_os(path), Level0Mosaic(path)
    # fully scanned (survey: identical, mean RGB 208.9, 142.7, 205.1)
    probe = src.read_level(0, 1440, 6048, 512, 512)
    np.testing.assert_array_equal(probe, oracle.read(1440, 6048, 512, 512)[..., :3])
    assert probe.reshape(-1, 3).mean(axis=0) == pytest.approx((208.89, 142.72, 205.13), abs=0.01)
    for x, y in ((700, 5700), (2500, 7400), (3000, 9500), (1500, 10500)):
        ours = src.read_level(0, x, y, 512, 512)
        theirs = oracle.read(x, y, 512, 512)
        white = np.where(theirs[..., 3:] == 255, theirs[..., :3], 255)  # unscanned: white
        diff = np.abs(ours.astype(np.int16) - white).max(axis=2)
        assert diff.mean() <= 0.1
        assert (diff > 2).mean() <= 0.001  # ties where two pieces overlap
    assert (src.read_level(0, 0, 0, 256, 256) == 255).all()  # never scanned: white


@pytest.mark.testdata
def test_mrxs_levels_are_read_in_their_own_grid() -> None:
    path = sample_path(MRXS)
    src = open_os(path)
    for level, (w, h) in enumerate(src.level_dimensions):
        x, y, bw, bh = w // 4, h // 3, min(w // 2, 600), min(h // 3, 600)
        whole = src.read_level(level, x, y, bw, bh)
        part = src.read_level(level, x + 5, y + 3, 40, 30)
        np.testing.assert_array_equal(part, whole[3:33, 5:45])  # no sub-pixel resampling
    # levels 1-3 are registered on level 0: 2^k means of level 0, shifted by nothing
    base = src.read_level(0, 1024, 5632, 2048, 2048).astype(np.float64)
    for level in (1, 2, 3):
        k, n = 2**level, 2048 // 2**level
        coarse = src.read_level(level, 1024 // k, 5632 // k, n, n).astype(np.float64)
        errors = []
        for dx in (0, k):
            means = np.roll(base, -dx, axis=1).reshape(n, k, n, k, 3).mean(axis=(1, 3))
            errors.append(np.abs(means - coarse)[1:-1, 1:-1].mean())
        assert errors[0] <= 7.0
        assert errors[0] < errors[1]


@pytest.mark.testdata
def test_mrxs_views_render_on_the_exact_level_grid() -> None:
    """The renderer places level 5 at 32 x its pixel, not at its extent ratio (32.05)."""
    src = open_os(sample_path(MRXS))
    mx, my = src.mpp
    x, y, w, h = 180, 300, 40, 50  # level-5 px near the far corner, where the two differ
    box = BBox(x * 32 * mx, y * 32 * my, (x + w) * 32 * mx, (y + h) * 32 * my)
    rendered = render_region(src, box, (w, h))
    assert_close(rendered, src.read_level(5, x, y, w, h), mean=0.05, peak=1)


@pytest.mark.testdata
def test_mrxs_read_error_reopens_the_handle(tmp_path: Path) -> None:
    path = copy_mrxs(tmp_path)
    data = path.with_suffix("") / "Data0000.dat"  # level 0
    data.write_bytes(data.read_bytes()[: data.stat().st_size // 2])
    src = open_os(path)
    expected = open_os(sample_path(MRXS)).read_level(5, 0, 0, 232, 484)
    with pytest.raises(SlideReadError, match="s1"):
        src.read_level(0, 600, 9000, 4300, 512)
    # OpenSlide's handle would now fail every call; a fresh one serves the intact levels
    np.testing.assert_array_equal(src.read_level(5, 0, 0, 232, 484), expected)


@pytest.mark.testdata
def test_mrxs_incomplete_sets_are_refused(tmp_path: Path) -> None:
    path = copy_mrxs(tmp_path)
    (path.with_suffix("") / "Data0007.dat").unlink()
    with pytest.raises(SlideNotFound, match="1 of its 22 files are missing"):
        open_os(path)
    shutil.rmtree(path.with_suffix(""))  # a lone .mrxs is only a preview JPEG
    with pytest.raises(SlideMetadataError, match="not a 3DHistech MRXS slide") as err:
        open_os(path, "mirax")
    assert "folder of the same name" in (err.value.hint or "")


@pytest.mark.testdata
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("SLIDE_TYPE_BRIGHTFIELD", "SLIDE_TYPE_FLUORESCENCE", "fluorescence slide"),
        ("SLIDE_TYPE = SLIDE_TYPE_BRIGHTFIELD", "X = Y", "not marked as a brightfield"),
        ("IMAGE_FORMAT = JPEG", "IMAGE_FORMAT = JPEGXR", "cannot decode"),
        (
            "HIER_COUNT = 3",
            "HIER_COUNT = 4\r\nHIER_3_NAME = Microscope focus level\r\nHIER_3_COUNT = 2",
            "2 focal planes",
        ),
        (
            "FILE_7 = Data0007.dat",
            "FILE_7 = ../Data0007.dat",
            "names a file SlideTwin will not open",
        ),
        ("FILE_COUNT = 19", "FILE_COUNT = many", "cannot be read \\(ValueError\\)"),
        ("[DATAFILE]", "[DATAFILES]", "cannot be read \\(KeyError\\)"),
    ],
)
def test_mrxs_variants_outside_the_validated_case(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = copy_mrxs(tmp_path)
    edit_slidedat(path, old, new)
    with pytest.raises(SlideMetadataError, match=message):
        open_os(path)


@pytest.mark.testdata
def test_mrxs_single_focus_level_is_served(tmp_path: Path) -> None:
    path = copy_mrxs(tmp_path)
    edit_slidedat(
        path,
        "HIER_COUNT = 3",
        "HIER_COUNT = 4\r\nHIER_3_NAME = Microscope focus level\r\nHIER_3_COUNT = 1",
    )
    assert open_os(path).level_dimensions[0] == (7436, 15494)


@pytest.mark.testdata
def test_mrxs_file_set_is_hashed_and_pinned_by_the_registry(tmp_path: Path) -> None:
    path = copy_mrxs(tmp_path)
    entry: dict[str, Any] = {"id": "mrxs1", "path": f"mrxs/{path.name}"}
    src = open_os(path)
    assert src.fileset is not None
    expected = fileset_sha256(*src.fileset, file_sha256)
    for pinned, error in ((expected, None), ("0" * 64, SlideIntegrityError)):
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(yaml.safe_dump({"slides": [{**entry, "fileset_sha256": pinned}]}))
        registry = SlideRegistry.from_manifest(manifest, tmp_path)
        if error is None:
            assert registry.get("mrxs1").sha256 == expected
        else:
            with pytest.raises(error):
                registry.get("mrxs1")
        registry.close()


# --- Hamamatsu NDPI (synthetic, the survey's byte layout) ---------------------------------------


def test_ndpi_levels_equal_pillow_decodes(tmp_path: Path) -> None:
    strips = write_ndpi(tmp_path / "a.ndpi")
    src = open_os(tmp_path / "a.ndpi")
    assert src.level_dimensions == (
        (1024, 768),
        (512, 384),
        (256, 192),
        (128, 96),
        (64, 48),
        (32, 24),
    )
    # levels 1, 3, 4 and 5 are OpenSlide's DCT-scaled decodes of the two stored levels
    expected = [
        decode(strips[(0, 0)]),
        decode(strips[(0, 0)], 2),
        *(decode(strips[(1, 0)], s) for s in (1, 2, 4, 8)),
    ]
    for level, image in enumerate(expected):
        h, w = image.shape[:2]
        assert_close(src.read_level(level, 0, 0, w, h), image)
    assert src.mpp == pytest.approx((1e4 / 21910, 1e4 / 21975), rel=1e-12)  # px/cm, level 0
    prov = src.provenance
    assert (prov["format"], prov["mpp_source"]) == ("hamamatsu-ndpi", "ndpi resolution tags")
    assert (prov["plane"], prov["planes"], prov["downsamples"]) == (0, 1, [1, 2, 4, 8, 16, 32])
    assert src.fileset is None  # a single file: the registry keeps its plain sha256


def test_ndpi_z_stack_serves_the_plane_at_offset_0(tmp_path: Path) -> None:
    strips = write_ndpi(tmp_path / "z.ndpi", planes=(-1000, 0, 1000))  # -1000 is the first page
    for plane in (None, 0):
        src = open_os(tmp_path / "z.ndpi", plane=plane)
        assert_close(src.read_level(0, 0, 0, 1024, 768), decode(strips[(0, 0)]))
        assert src.provenance["planes"] == 3
    with pytest.raises(SlideMetadataError, match="plane 0 only"):
        open_os(tmp_path / "z.ndpi", plane=2)


def test_ndpi_without_mcu_starts_reads_the_same(tmp_path: Path) -> None:
    strips = write_ndpi(tmp_path / "n.ndpi", mcu_starts=False)
    assert_close(
        open_os(tmp_path / "n.ndpi").read_level(0, 0, 0, 1024, 768), decode(strips[(0, 0)])
    )


@pytest.mark.parametrize(
    "tag",
    [
        (259, SHORT, 22610),  # JPEG XR
        (277, SHORT, 1),  # one sample per pixel
        (65425, LONG, 1),  # a second tissue region
        (65434, LONG, 1),  # fluorescence
    ],
)
def test_ndpi_variants_without_a_sample_are_refused(tmp_path: Path, tag: Any) -> None:
    write_ndpi(tmp_path / "v.ndpi", page_tags=[tag])
    with pytest.raises(SlideMetadataError, match="NDPI variant SlideTwin has not validated"):
        open_os(tmp_path / "v.ndpi")


@pytest.mark.parametrize(
    ("level1", "kept"),
    [
        # OpenSlide adds level 0 at 1/4 (exact), then the stored 255 x 192 (4.008, resampled) and
        # its 1/2 decode, 128 x 96: 1024 / 8 by its size, but 8.03 in truth. Both are dropped.
        ((255, 192), [1, 2, 4]),
        ((256, 191), [1, 2]),
    ],
)
def test_ndpi_levels_after_a_fractional_downsample_are_dropped(
    tmp_path: Path, level1: tuple[int, int], kept: list[int]
) -> None:
    strips = write_ndpi(tmp_path / "f.ndpi", level1=level1)
    src = open_os(tmp_path / "f.ndpi")
    assert src.provenance["downsamples"] == kept
    assert src.level_dimensions == tuple((1024 // k, 768 // k) for k in kept)
    for level, k in enumerate(kept):
        image = decode(strips[(0, 0)], k)  # all decoded from level 0
        assert_close(src.read_level(level, 0, 0, 1024 // k, 768 // k), image)


def test_exact_levels() -> None:
    # 1001 / 2 rounded up, 1001 / 4 rounded down; 76 is not 600 / 8; 63 x 38 comes after it
    dims = ((1001, 600), (501, 300), (250, 150), (125, 76), (63, 38))
    kept = exact_levels(dims, (1.0, 2.0, 4.0, 8.0, 16.0))
    assert kept == [(0, 1, (1001, 600)), (1, 2, (501, 300)), (2, 4, (250, 150))]
    assert exact_levels(((8, 8), (3, 3), (2, 2)), (1.0, 2.667, 4.0)) == [(0, 1, (8, 8))]
    assert exact_levels(((8, 8), (16, 16)), (1.0, 0.5)) == [(0, 1, (8, 8))]


@pytest.mark.parametrize("px_per_cm", [None, (10, 10), (21910, 1)])
def test_ndpi_untrusted_mpp_needs_an_override(tmp_path: Path, px_per_cm: Any) -> None:
    write_ndpi(tmp_path / "r.ndpi", px_per_cm=px_per_cm)
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_os(tmp_path / "r.ndpi")
    src = open_os(tmp_path / "r.ndpi", mpp_override=(0.25, 0.25))
    assert (src.mpp, src.provenance["mpp_source"]) == ((0.25, 0.25), "mpp_override")


def test_files_openslide_does_not_recognise_are_refused(tmp_path: Path) -> None:
    levels = pyramid(line_image(256, 256, (10,), ()), 2)
    tiff = write_pyramid_tiff(tmp_path / "g.ndpi", levels, 0.5)
    with pytest.raises(SlideMetadataError, match="not a Hamamatsu NDPI slide"):
        open_os(tiff, "hamamatsu-ndpi")
    with pytest.raises(SlideMetadataError, match="does not handle aperio-svs"):
        open_os(tiff, "aperio-svs")


def test_ndpi_that_tifffile_cannot_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_ndpi(tmp_path / "a.ndpi")

    def broken(*args: Any, **kw: Any) -> Any:
        raise tifffile.TiffFileError("damaged")

    monkeypatch.setattr(reader.tifffile, "TiffFile", broken)
    with pytest.raises(SlideMetadataError, match="tifffile backend \\(TiffFileError\\)"):
        open_os(tmp_path / "a.ndpi", "hamamatsu-ndpi")


def test_options_without_use_are_refused(tmp_path: Path) -> None:
    write_ndpi(tmp_path / "a.ndpi")
    with pytest.raises(SlideMetadataError, match="no scene or channels to choose"):
        open_os(tmp_path / "a.ndpi", scene=1, channels=(0,))
    index = write_vms(tmp_path / "vms")
    with pytest.raises(SlideMetadataError, match="no plane to choose"):
        open_os(index, plane=0)


@pytest.mark.testdata
def test_cmu1_ndpi_golden_regions() -> None:
    """The survey's golden values (sha256 prefixes of the raw RGB bytes) on the real CC0 file."""
    src = open_os(sample_path("Hamamatsu/CMU-1.ndpi", tier=3))
    assert len(src.level_dimensions) == 9  # 4 stored levels and 5 DCT-scaled ones, all exact
    widths = [w for w, _ in src.level_dimensions]
    for width, (x, y), prefix in (
        (51200, (25344, 18816), "ce30a00bf6fbcebc"),
        (12800, (6144, 4512), "c2b8ad442304f9e6"),
        (800, (0, 0), "22aa4a50a587b12e"),
    ):
        region = src.read_level(widths.index(width), x, y, 512, 512)
        assert hashlib.sha256(region.tobytes()).hexdigest().startswith(prefix)


# --- Hamamatsu VMS and VMU (synthetic) ----------------------------------------------------------


def test_vms_grid_levels_equal_pillow_decodes(tmp_path: Path) -> None:
    index = write_vms(tmp_path / "vms", cols=(1024, 512), rows=(512, 256), mpp=(0.2282, 0.2275))
    src = open_os(index)
    assert src.level_dimensions == tuple((1536 // 2**k, 768 // 2**k) for k in range(7))
    folder = index.parent
    cells = {
        (c, r): (folder / ("s.jpg" if (c, r) == (0, 0) else f"s({c},{r}).jpg")).read_bytes()
        for c in range(2)
        for r in range(2)
    }
    for level, scale in enumerate((1, 2, 4)):  # the JPEG grid, decoded at 1/1, 1/2 and 1/4
        rows = [
            np.concatenate([decode(cells[(c, r)], scale) for c in range(2)], 1) for r in range(2)
        ]
        level_px = src.read_level(level, 0, 0, 1536 // scale, 768 // scale)
        assert_close(level_px, np.concatenate(rows))
    mapped = (folder / "s_map2.jpg").read_bytes()
    for level, scale in zip(range(3, 7), (1, 2, 4, 8), strict=True):  # the map, 1/8 of level 0
        image = decode(mapped, scale)
        assert_close(src.read_level(level, 0, 0, image.shape[1], image.shape[0]), image)
    assert src.mpp == pytest.approx((0.2282, 0.2275))
    assert src.provenance["mpp_source"] == "vms PhysicalWidth/PhysicalHeight"
    assert src.fileset == (
        folder,
        (
            "s(0,1).jpg",
            "s(1,0).jpg",
            "s(1,1).jpg",
            "s.jpg",
            "s.opt",
            "s.vms",
            "s_macro.jpg",
            "s_map2.jpg",
        ),
    )


def test_vms_macro_pixels_are_never_served(tmp_path: Path) -> None:
    index = write_vms(tmp_path / "vms")
    src = open_os(index)
    level0 = src.read_level(0, 0, 0, 1024, 512)
    assert_close(level0, decode((index.parent / "s.jpg").read_bytes()))
    green = (level0[..., 1] > 240) & (level0[..., 0] < 15) & (level0[..., 2] < 15)
    assert not green.any()  # the canary macro is pure green


@pytest.mark.parametrize("missing", ["s.opt", "s_macro.jpg", "s_map2.jpg", "s.jpg"])
def test_vms_incomplete_sets_are_refused(tmp_path: Path, missing: str) -> None:
    index = write_vms(tmp_path / "vms")
    (index.parent / missing).unlink()
    with pytest.raises(SlideNotFound, match="1 of its 5 files are missing"):
        open_os(index)


def test_vms_without_optimisation_and_macro_entries(tmp_path: Path) -> None:
    index = write_vms(tmp_path / "vms", optimisation=False, macro=False)
    src = open_os(index)
    assert src.fileset is not None
    assert src.fileset[1] == ("s.jpg", "s.vms", "s_map2.jpg")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["NoLayers=2"], "2 focal layers"),
        (["NoLayers=two"], "cannot be read \\(ValueError\\)"),
        (["ImageFile(1,1)=../x.jpg"], "names a file SlideTwin will not open"),
        (["ImageFile(1,1)=x\udcff.jpg"], "not a valid UTF-8 name"),
    ],
)
def test_vms_index_outside_the_validated_case(
    tmp_path: Path, extra: list[str], message: str
) -> None:
    index = write_vms(tmp_path / "vms")
    text = index.read_bytes().decode("utf-8")
    lines = [ln for ln in text.split("\r\n") if not ln.startswith("NoLayers")]
    raw = "\r\n".join(lines[:1] + extra + lines[1:]).encode("utf-8", "surrogateescape")
    index.write_bytes(raw)
    with pytest.raises(SlideMetadataError, match=message):
        open_os(index)


def test_vms_damaged_map_cannot_be_opened(tmp_path: Path) -> None:
    index = write_vms(tmp_path / "vms")
    (index.parent / "s_map2.jpg").write_bytes(b"\xff\xd8 not a JPEG")
    with pytest.raises(SlideMetadataError, match="openslide backend \\(OpenSlideError\\)"):
        open_os(index)


def test_vms_read_error_reopens_the_handle(tmp_path: Path) -> None:
    index = write_vms(tmp_path / "vms", cols=(512, 512))
    cell = index.parent / "s(1,0).jpg"
    cell.write_bytes(cell.read_bytes()[: cell.stat().st_size // 2])  # the right-hand cell
    src = open_os(index)
    with pytest.raises(SlideReadError, match="level 0"):
        src.read_level(0, 512, 256, 512, 256)
    left = decode((index.parent / "s.jpg").read_bytes())
    assert_close(src.read_level(0, 0, 0, 512, 512), left)  # a new handle reads the intact cell


def test_vmu_is_experimental(tmp_path: Path) -> None:
    index, level0, small = write_vmu(tmp_path / "vmu")
    with pytest.raises(SlideMetadataError, match="Hamamatsu VMU slide, a format validated on"):
        open_os(index)
    src = open_os(index, experimental=True)
    assert src.level_dimensions == ((512, 256), (64, 32))  # the image and its map
    # 12-bit samples become 8-bit by dropping the 4 low bits, exactly
    np.testing.assert_array_equal(src.read_level(0, 0, 0, 512, 256), level0)
    np.testing.assert_array_equal(src.read_level(1, 0, 0, 64, 32), small)
    prov = src.provenance
    assert (prov["format"], prov["variant"]) == ("hamamatsu-vms", "vmu")
    assert prov["experimental"] is True
    assert src.fileset is not None
    assert src.fileset[1] == ("u.ngr", "u.vmu", "u_macro.jpg", "u_map.ngr")


# --- Sakura SVSlide (synthetic, older schema) ---------------------------------------------------


def test_svslide_is_experimental(tmp_path: Path) -> None:
    image = vms_image(1024, 1024, seed=3)
    levels = write_svslide(tmp_path / "x.svslide", image)
    path = tmp_path / "x.svslide"
    with pytest.raises(SlideMetadataError, match="Sakura SVSlide slide, a format validated on"):
        open_os(path)
    src = open_os(path, experimental=True)
    assert src.level_dimensions == ((1024, 1024), (512, 512), (256, 256))
    for level, written in enumerate(levels):
        h, w = written.shape[:2]
        # one grayscale JPEG per channel and tile (quality 95)
        assert_close(src.read_level(level, 0, 0, w, h), written, mean=2.0, peak=40)
    assert src.mpp == pytest.approx((0.499, 0.499))
    prov = src.provenance
    assert (prov["mpp_source"], prov["experimental"]) == ("svslide ResolutionMmPerPix", True)
    assert src.fileset is None


def test_other_sqlite_files_are_refused(tmp_path: Path) -> None:
    import sqlite3

    db = sqlite3.connect(tmp_path / "other.svslide")
    db.execute("CREATE TABLE gigapixel_images (id integer)")  # the newer Sakura schema, say
    db.commit()
    db.close()
    with pytest.raises(SlideMetadataError, match="not a Sakura SVSlide slide") as err:
        open_os(tmp_path / "other.svslide")
    assert "older SVSlide schema" in (err.value.hint or "")


def test_open_source_routes_openslide_formats(tmp_path: Path) -> None:
    write_ndpi(tmp_path / "a.ndpi")
    src = open_source(tmp_path / "a.ndpi", sniff(tmp_path / "a.ndpi"), name="s1")
    assert isinstance(src, OpenSlideFamilySource)


# --- index parsing and the handle's life cycle, where no sample can fail on demand --------------


def test_index_files_that_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / "junk.ini").write_bytes(b"no section header\n")
    for path in (tmp_path / "junk.ini", tmp_path / "missing.ini"):
        with pytest.raises(SlideMetadataError, match="index file that cannot be read"):
            reader._read_ini(path, "s1")
    text = b"\xef\xbb\xbf[A]\r\nKey=first\r\n  Other = x = y\r\n#c\r\nKey=last\r\n"
    (tmp_path / "ok.ini").write_bytes(text)
    ini = reader._read_ini(tmp_path / "ok.ini", "s1")
    # an indented line is a key (GLib), not the continuation of Key's value (configparser)
    assert (ini["A"]["Key"], ini["A"]["Other"]) == ("last", "x = y")


class Recorded(dict[str, str]):
    """Properties that remember which keys were read."""

    def __init__(self, items: dict[str, str]) -> None:
        super().__init__(items)
        self.asked: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.asked.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self.asked.add(key)
        return super().__getitem__(key)


class FakeSlide:
    """Stands in for openslide.OpenSlide: fails on demand and records the reads it was asked for.
    The fixture `fake` restores every class attribute after each test."""

    pyramid: Any = (((64, 48), (32, 24)), (1.0, 2.0))
    mpp = ("0.5", "0.5")
    fail_open = False
    handles: ClassVar[list[FakeSlide]] = []

    def __init__(self, path: str) -> None:
        if FakeSlide.fail_open:
            raise openslide.OpenSlideError("cannot open")
        self.level_dimensions, self.level_downsamples = FakeSlide.pyramid
        mpp = dict(zip(("openslide.mpp-x", "openslide.mpp-y"), FakeSlide.mpp, strict=True))
        self.properties = Recorded({**mpp, "hamamatsu.Reference": "A1 (a slide label)"})
        self.broken = False
        self.closed = False
        self.reads: list[tuple[Any, ...]] = []
        FakeSlide.handles.append(self)

    def read_region(self, location: Any, level: int, size: Any) -> Image.Image:
        if self.broken:
            raise openslide.OpenSlideError("latched")
        self.reads.append((location, level, size))
        return Image.new("RGBA", size, (10, 20, 30, 255))

    def close(self) -> None:
        self.closed = True

    @property
    def associated_images(self) -> Any:
        raise AssertionError("associated images (label, macro) are never read")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeSlide]:
    for attr in ("pyramid", "mpp", "fail_open"):
        monkeypatch.setattr(FakeSlide, attr, getattr(FakeSlide, attr))
    monkeypatch.setattr(FakeSlide, "handles", [])
    monkeypatch.setattr(reader.openslide, "OpenSlide", FakeSlide)
    return FakeSlide


def fake_source(tmp_path: Path) -> OpenSlideFamilySource:
    layout = reader._Layout("fake")
    return OpenSlideFamilySource(tmp_path / "f", name="s1", fmt="hamamatsu-ndpi", layout=layout)


def test_reads_use_level_coordinates_times_the_downsample(
    tmp_path: Path, fake: type[FakeSlide]
) -> None:
    src = fake_source(tmp_path)
    assert (src.read_level(1, 3, 5, 7, 9) == (10, 20, 30)).all()
    assert fake.handles[0].reads == [((6, 10), 1, (7, 9))]
    # of the properties only the resolution is read; labels and names never are
    assert fake.handles[0].properties.asked == {"openslide.mpp-x", "openslide.mpp-y"}


def test_a_failed_read_is_followed_by_a_fresh_handle(tmp_path: Path, fake: type[FakeSlide]) -> None:
    src = fake_source(tmp_path)
    fake.handles[0].broken = True
    with pytest.raises(SlideReadError, match="OpenSlideError"):
        src.read_level(0, 0, 0, 4, 4)
    assert fake.handles[0].closed
    fake.fail_open = True  # reopening fails too: that read fails, and the next one tries again
    with pytest.raises(SlideReadError):
        src.read_level(0, 0, 0, 4, 4)
    fake.fail_open = False
    assert src.read_level(0, 0, 0, 4, 4).shape == (4, 4, 3)
    assert len(fake.handles) == 2


def test_a_changed_file_is_not_read(tmp_path: Path, fake: type[FakeSlide]) -> None:
    src = fake_source(tmp_path)
    fake.handles[0].broken = True
    with pytest.raises(SlideReadError):
        src.read_level(0, 0, 0, 4, 4)
    fake.pyramid = (((64, 48),), (1.0,))  # replaced on disk by another slide
    with pytest.raises(SlideReadError, match="RuntimeError"):
        src.read_level(0, 0, 0, 4, 4)
    assert fake.handles[1].closed
    assert not fake.handles[1].reads


def test_closed_sources_do_not_reopen(tmp_path: Path, fake: type[FakeSlide]) -> None:
    src = fake_source(tmp_path)
    src.close()
    src.close()
    assert fake.handles[0].closed
    with pytest.raises(SlideReadError):
        src.read_level(0, 0, 0, 4, 4)
    assert len(fake.handles) == 1


def test_open_failures_are_reported_by_type(tmp_path: Path, fake: type[FakeSlide]) -> None:
    fake.fail_open = True
    with pytest.raises(SlideMetadataError, match="openslide backend \\(OpenSlideError\\)"):
        fake_source(tmp_path)
    fake.fail_open = False
    fake.pyramid = ((), ())  # no level at all
    with pytest.raises(SlideMetadataError, match="openslide backend \\(IndexError\\)"):
        fake_source(tmp_path)
    assert fake.handles[-1].closed


@pytest.mark.parametrize("mpp", [("n/a", "0.5"), ("inf", "0.5"), ("0.5", "40")])
def test_unusable_mpp_values(tmp_path: Path, fake: type[FakeSlide], mpp: tuple[str, str]) -> None:
    fake.mpp = mpp
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        fake_source(tmp_path)
    assert fake.handles[-1].closed
