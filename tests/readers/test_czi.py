"""Zeiss CZI reader (M8): the compositor's rules, scene choice, refusals, parity with OpenSlide and
czifile."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import czifile
import numpy as np
import pytest
from czifile import CziCompressionType, CziPixelType

import slidetwin.readers.czi as czi_reader
from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.czi import CziSource, Layer, Tile, compose, open_reader
from tests.conftest import sample_path

MATCH = FormatMatch("zeiss-czi", "czi")
PREVIEW = "Zeiss/Zeiss-5-SlidePreview-{}.czi"


@dataclass(frozen=True)
class Entry:
    """Stands in for czifile's directory entry: the fields the reader looks at."""

    start: tuple[int, ...]
    shape: tuple[int, ...]
    stored_shape: tuple[int, ...]
    scale: tuple[Any, ...]
    dims: tuple[str, ...] = ("C", "Y", "X", "S")
    mosaic_index: int = -1
    scene_index: int = -1
    pixel_type: CziPixelType = CziPixelType.BGR24
    compression: CziCompressionType = CziCompressionType.JPEGXR
    file_part: int = 0
    file_position: int = 0

    @property
    def stop(self) -> tuple[int, ...]:
        return tuple(a + b for a, b in zip(self.start, self.shape, strict=True))


def entry(x: int, y: int, w: int, h: int, k: int = 0, **kw: Any) -> Entry:
    """A subblock at level-0 position (x, y) covering w x h level-0 pixels, stored at 2^k."""
    f = 2**k
    return Entry((0, y, x, 0), (1, h, w, 3), (1, h // f, w // f, 3), (1, f, f, 1), **kw)


def solid(value: int, h: int = 4, w: int = 4, dtype: Any = np.uint8) -> np.ndarray:
    return np.full((h, w, 3), value, dtype)


# --- the compositor ---------------------------------------------------------------------------


def test_highest_mosaic_index_is_drawn_on_top() -> None:
    tiles = [
        Tile(0, 0, 4, 4, (2, 10), solid(30)),
        Tile(2, 0, 4, 4, (0, 20), solid(10)),
        Tile(1, 0, 4, 4, (1, 5), solid(20)),
    ]
    layer = Layer(tiles)
    assert [t.order[0] for t in layer.tiles] == [0, 1, 2]
    out = compose(layer.overlapping(0, 0, 6, 4), 0, 0, 6, 4, lambda t: (t.entry, None))
    assert out[0].tolist() == [[30] * 3] * 4 + [[20] * 3] + [[10] * 3]  # M 2 over 1 over 0


def test_masked_pixels_are_not_drawn() -> None:
    mask = np.ones((4, 4), bool)
    mask[:, 2:] = False
    tiles = {"low": solid(10), "high": solid(90)}
    layer = Layer([Tile(0, 0, 4, 4, (0, 0), "low"), Tile(0, 0, 4, 4, (1, 0), "high")])

    def load(tile: Tile) -> tuple[np.ndarray, np.ndarray | None]:
        return tiles[tile.entry], mask if tile.entry == "high" else None

    out = compose(layer.tiles, 0, 0, 4, 4, load)
    assert out[0, :, 0].tolist() == [90, 90, 10, 10]
    wrong = np.ones((3, 3), bool)  # a mask that does not fit its tile is not used
    out = compose(layer.tiles[1:], 0, 0, 4, 4, lambda t: (solid(90), wrong))
    assert (out == 90).all()


def test_placement_clipping_and_fill() -> None:
    layer = Layer([Tile(-2, -1, 4, 4, (0, 0), "a"), Tile(5, 5, 4, 4, (1, 0), "b")])
    assert layer.overlapping(10, 10, 2, 2) == []
    assert [t.entry for t in layer.overlapping(0, 0, 3, 3)] == ["a"]
    data = np.arange(16, dtype=np.uint8).reshape(4, 4, 1).repeat(3, axis=2)
    out = compose(layer.tiles, 0, 0, 3, 3, lambda t: (data, None))
    assert out[:, :, 0].tolist() == [[6, 7, 255], [10, 11, 255], [14, 15, 255]]
    short = compose(layer.tiles[1:], 4, 4, 2, 2, lambda t: (solid(1, 0, 0), None))  # decoded empty
    assert (short == 255).all()


def test_wide_samples_are_shifted_and_clipped() -> None:
    data = np.array([[[0, 255 << 6, 300 << 6]]], np.uint16)  # 14-bit data, one value over range
    out = compose([Tile(0, 0, 1, 1, (0, 0))], 0, 0, 1, 1, lambda t: (data, None), shift=6)
    assert out.tolist() == [[[0, 255, 255]]]


# --- scenes, layout and refusals (directory entries stand in for a file) ----------------------


def test_scene_choice() -> None:
    plain = [entry(0, 0, 8, 8)]
    assert czi_reader._scene_entries(plain, None, "s1") == (0, plain)
    two = [entry(0, 0, 8, 8, scene_index=0), entry(9, 0, 8, 8, scene_index=1)]
    assert czi_reader._scene_entries(two, 1, "s1") == (1, two[1:])
    with pytest.raises(SlideMetadataError, match=r"holds 2 scenes \(0, 1\)") as err:
        czi_reader._scene_entries(two, None, "s1")
    assert "scene" in (err.value.hint or "")
    with pytest.raises(SlideMetadataError, match="has no scene 2; its scenes are 0, 1"):
        czi_reader._scene_entries(two, 2, "s1")
    with pytest.raises(SlideMetadataError, match="belong to no scene"):
        czi_reader._scene_entries([*plain, *two], 0, "s1")
    with pytest.raises(SlideMetadataError, match="no image subblocks"):
        czi_reader._scene_entries([], None, "s1")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"dims": ("C", "X", "Y", "S")}, "unexpected CZI subblock layout"),
        ({"file_part": 1}, "multi-file CZI"),
        ({"pixel_type": CziPixelType.GRAY16}, "GRAY16 pixels; SlideTwin reads brightfield"),
        ({"compression": CziCompressionType.JPEG}, "JPEG compression, which SlideTwin does not"),
        ({"compression": CziCompressionType.UNCOMPRESSED}, "checked on synthetic files only"),
    ],
)
def test_refused_entries(change: dict[str, Any], message: str) -> None:
    entries = [entry(0, 0, 8, 8), replace(entry(8, 0, 8, 8), **change)]
    with pytest.raises(SlideMetadataError, match=message):
        czi_reader._check_entries(entries, "s1", experimental=False)


def test_uncompressed_is_experimental() -> None:
    entries = [entry(0, 0, 8, 8, compression=CziCompressionType.UNCOMPRESSED)]
    assert czi_reader._check_entries(entries, "s1", experimental=True) == CziPixelType.BGR24
    mixed = [entry(0, 0, 8, 8), entry(0, 0, 8, 8, pixel_type=CziPixelType.BGR48)]
    with pytest.raises(SlideMetadataError, match="BGR24, BGR48 pixels"):
        czi_reader._check_entries(mixed, "s1", experimental=False)


@pytest.mark.parametrize(
    ("dims", "starts", "shape", "message"),
    [
        (("C", "Y", "X", "S"), [(0,), (1,)], (1,), "several channels"),
        (("Z", "Y", "X", "S"), [(0,), (1,)], (1,), "several focal planes"),
        (("T", "Y", "X", "S"), [(0,), (0,)], (2,), "several time points"),
        (("H", "Y", "X", "S"), [(0,), (3,)], (1,), "several planes along dimension H"),
    ],
)
def test_one_plane_only(
    dims: tuple[str, ...], starts: list[tuple[int]], shape: tuple[int], message: str
) -> None:
    entries = [
        Entry((*s, 0, 0, 0), (*shape, 8, 8, 3), (*shape, 8, 8, 3), (1, 1, 1, 1), dims=dims)
        for s in starts
    ]
    with pytest.raises(SlideMetadataError, match=message):
        czi_reader._check_entries(entries, "s1", experimental=False)


def test_levels_floor_sizes_and_placement() -> None:
    entries = [
        entry(-100, 50, 60, 40, mosaic_index=1),
        entry(-50, 60, 60, 40, mosaic_index=0),
        entry(-101, 49, 64, 52, k=1),  # starts left of the scene: placed at floor(-1 / 2)
        entry(-100, 50, 128, 64, k=6),  # coarser than one pixel: left out
    ]
    levels = czi_reader._levels(entries, "s1")
    assert [(k, size) for k, size, _ in levels] == [(0, (110, 50)), (1, (55, 25))]
    assert [(t.x, t.y, t.order[0]) for t in levels[0][2].tiles] == [(50, 10, 0), (0, 0, 1)]
    assert [(t.x, t.y, t.w, t.h) for t in levels[1][2].tiles] == [(-1, -1, 32, 26)]


@pytest.mark.parametrize("scale", [(1, 2, 1, 1), (1, 3, 3, 1), (1, 1.5, 1.5, 1), (1, 0.5, 0.5, 1)])
def test_levels_need_power_of_two_scales(scale: tuple[Any, ...]) -> None:
    entries = [entry(0, 0, 8, 8), replace(entry(0, 0, 8, 8), scale=scale)]
    with pytest.raises(SlideMetadataError, match="scale is not a power of two"):
        czi_reader._levels(entries, "s1")


def test_levels_need_full_resolution() -> None:
    with pytest.raises(SlideMetadataError, match="no full-resolution subblocks"):
        czi_reader._levels([entry(0, 0, 8, 8, k=1)], "s1")


def xml(body: str) -> ElementTree.Element:
    return ElementTree.fromstring(f"<ImageDocument><Metadata>{body}</Metadata></ImageDocument>")


def test_bit_shift() -> None:
    image = "<Information><Image><ComponentBitCount>{}</ComponentBitCount></Image></Information>"
    channel = (
        "<Information><Image><Dimensions><Channels><Channel>"
        "<ComponentBitCount>12</ComponentBitCount></Channel></Channels></Dimensions></Image>"
        "</Information>"
    )
    bgr48 = CziPixelType.BGR48
    assert czi_reader._bit_shift(CziPixelType.BGR24, None, "s1") == 0
    assert czi_reader._bit_shift(bgr48, xml(image.format(16)), "s1") == 8
    assert czi_reader._bit_shift(bgr48, xml(image.format(14)), "s1") == 6
    assert czi_reader._bit_shift(bgr48, xml(channel), "s1") == 4
    for root in (None, xml(""), xml(image.format("x")), xml(image.format(17))):
        with pytest.raises(SlideMetadataError, match="without a usable bit count"):
            czi_reader._bit_shift(bgr48, root, "s1")


def test_scaling_mpp() -> None:
    items = "<Scaling><Items><Distance Id='X'><Value>{}</Value></Distance>{}</Items></Scaling>"
    y = "<Distance Id='Y'><Value>2.5E-07</Value></Distance>"
    assert czi_reader._scaling_mpp(xml(items.format("5E-07", y))) == pytest.approx((0.5, 0.25))
    assert czi_reader._scaling_mpp(xml(items.format("5E-07", ""))) is None
    assert czi_reader._scaling_mpp(xml(items.format("five", y))) is None
    assert czi_reader._scaling_mpp(None) is None


# --- whole files ------------------------------------------------------------------------------


def preview(kind: str) -> Path:
    return sample_path(PREVIEW.format(kind))


@pytest.mark.testdata
def test_preview_codecs_agree_and_match_openslide() -> None:
    import openslide

    reads = {}
    for kind in ("Zstd0", "Zstd1-HiLo", "JXR"):
        src = CziSource(preview(kind), name="s1", mpp_override=(0.5, 0.5))
        assert src.level_dimensions == ((1260, 615),)
        assert src.provenance["bit_shift"] == 8  # Bgr48 with ComponentBitCount 16
        reads[kind] = src.read_level(0, 0, 0, 1260, 615)
        src.close()
    np.testing.assert_array_equal(reads["Zstd0"], reads["Zstd1-HiLo"])
    lossy = np.abs(reads["JXR"].astype(np.int16) - reads["Zstd0"].astype(np.int16))
    assert lossy.mean() < 0.6  # JPEG XR is lossy
    assert lossy.max() <= 2
    for kind in ("Zstd0", "Zstd1-HiLo"):  # OpenSlide reads zstd CZI (not JPEG XR)
        with openslide.OpenSlide(preview(kind)) as slide:
            ref = np.asarray(slide.read_region((0, 0), 0, (1260, 615)))[..., :3]
        np.testing.assert_array_equal(reads[kind], ref)


@pytest.mark.testdata
def test_preview_mpp_is_implausible_without_an_override() -> None:
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        CziSource(preview("Zstd0"), name="s1")  # a slide photo at 35.9 um/px
    src = CziSource(preview("Zstd0"), name="s1", mpp_override=(0.5, 0.5))
    prov = src.provenance
    assert (prov["reader"], prov["format"], prov["scene"], prov["mpp_source"]) == (
        "czifile",
        "zeiss-czi",
        0,
        "mpp_override",
    )
    assert prov["fill"] == [255, 255, 255]
    assert set(prov["versions"]) == {"czifile", "imagecodecs"}
    with pytest.raises(SlideMetadataError, match="has no scene 1; its scenes are 0"):
        CziSource(preview("Zstd0"), name="s1", scene=1, mpp_override=(0.5, 0.5))


@pytest.mark.testdata
def test_read_failures_name_only_the_slide(monkeypatch: pytest.MonkeyPatch) -> None:
    src = CziSource(preview("Zstd0"), name="s1", mpp_override=(0.5, 0.5))

    def broken(tile: Tile) -> Any:
        raise OSError(f"cannot read {preview('Zstd0')}")

    monkeypatch.setattr(src, "_load", broken)
    with pytest.raises(SlideReadError, match="Slide 's1' could not be decoded at level 0") as err:
        src.read_level(0, 0, 0, 8, 8)
    assert "Zeiss" not in str(err.value)


def test_damaged_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.czi"
    junk.write_bytes(b"ZISRAWFILE" + b"\x00" * 6)
    with pytest.raises(SlideMetadataError, match=r"opened with the czifile backend"):
        CziSource(junk, name="s1")


@pytest.mark.testdata
def test_refusals_close_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    real_close = czifile.CziFile.close

    def close(self: Any) -> None:
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(czifile.CziFile, "close", close)
    gray = [entry(0, 0, 8, 8, pixel_type=CziPixelType.GRAY16)]
    monkeypatch.setattr(czifile.CziFile, "subblock_directory", property(lambda self: gray))
    with pytest.raises(SlideMetadataError, match="stores GRAY16 pixels") as err:
        CziSource(preview("Zstd0"), name="s1")
    assert "Fluorescence" in (err.value.hint or "")

    def boom(self: Any) -> Any:
        raise RuntimeError("damaged directory")

    monkeypatch.setattr(czifile.CziFile, "subblock_directory", property(boom))
    with pytest.raises(SlideMetadataError, match=r"czifile backend \(RuntimeError\)"):
        CziSource(preview("Zstd0"), name="s1")
    assert closed == [True, True]


@pytest.mark.testdata
def test_a_part_of_a_multi_file_czi_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Part(czifile.CziFile):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.header.file_part = 1

    monkeypatch.setattr(czi_reader.czifile, "CziFile", Part)
    with pytest.raises(SlideMetadataError, match="multi-file CZI"):
        CziSource(preview("Zstd0"), name="s1", mpp_override=(0.5, 0.5))


@pytest.mark.testdata
def test_open_source_and_options() -> None:
    path = preview("Zstd0")
    assert sniff(path) == MATCH
    src = open_source(path, MATCH, name="s1", mpp_override=(0.5, 0.5))
    assert isinstance(src, CziSource)
    with pytest.raises(SlideMetadataError, match="has no plane to choose"):
        open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions(plane=0))


# --- Zeiss-5-JXR: two scenes, overlapping frames, pyramid --------------------------------------


@pytest.fixture(scope="module")
def jxr() -> Path:
    return sample_path("Zeiss/Zeiss-5-JXR.czi", tier=2)


@pytest.mark.testdata
def test_jxr_needs_a_scene_choice(jxr: Path) -> None:
    with pytest.raises(SlideMetadataError, match=r"holds 2 scenes \(0, 1\)"):
        CziSource(jxr, name="s1")


@pytest.mark.testdata
def test_jxr_scenes_levels_and_mpp(jxr: Path) -> None:
    src = CziSource(jxr, name="s1", scene=0)
    assert src.level_dimensions == (
        (11323, 11338),
        (5661, 5669),
        (2830, 2834),
        (1415, 1417),
        (707, 708),
    )
    assert src.mpp == (0.34591494502960335, 0.34591494502960335)
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(5))
    prov = src.provenance
    assert (prov["scene"], prov["mpp_source"], prov["bit_shift"]) == (
        0,
        "czi Scaling Distance",
        0,
    )
    region = src.read_level(0, 5405, 5413, 512, 512)  # survey: the same in every RGB reader
    assert region.reshape(-1, 3).mean(0) == pytest.approx([105.86, 94.10, 110.67], abs=0.01)
    gap = (10816, 11072, 256, 256)  # no camera frame of scene 0 touches it
    with czifile.CziFile(jxr) as czi:
        frames = [e for e in czi.subblock_directory if e.scene_index == 0 and e.scale[2] == 1]
        x0, y0 = czi.scenes[0].bbox[:2]
    for e in frames:
        assert not (
            e.start[2] - x0 < gap[0] + gap[2]
            and e.stop[2] - x0 > gap[0]
            and e.start[1] - y0 < gap[1] + gap[3]
            and e.stop[1] - y0 > gap[1]
        )
    assert (src.read_level(0, *gap) == 255).all()
    other = CziSource(jxr, name="s1", scene=1)
    assert other.level_dimensions[0] == (11300, 11335)
    assert other.provenance["scene"] == 1


@pytest.mark.testdata
def test_jxr_level_0_matches_czifile_in_zeiss_order(jxr: Path) -> None:
    """czifile composes overlapping frames in mosaic order when a selection and an roi are given
    together; the survey found that mode equal to libCZI. The directory is not in that order."""
    src = CziSource(jxr, name="s1", scene=1)
    rng = np.random.default_rng(7)
    with czifile.CziFile(jxr) as czi:
        image = czi.scenes[1]
        x0, y0 = image.bbox[:2]
        for _ in range(6):
            x, y = int(rng.integers(0, 11300 - 512)), int(rng.integers(0, 11335 - 512))
            try:
                ref = image(C=0, roi=(x0 + x, y0 + y, 512, 512)).asarray(
                    fillvalue=255, maxworkers=1
                )
            except ValueError:  # czifile: the roi touches no subblock
                ref = np.full((512, 512, 3), 255, np.uint8)
            np.testing.assert_array_equal(src.read_level(0, x, y, 512, 512), ref)


@pytest.mark.testdata
def test_jxr_pyramid_level_is_aligned_with_level_0(jxr: Path) -> None:
    """Level 1 against the 2 x 2 block mean of level 0: no shift fits better than none."""
    src = CziSource(jxr, name="s1", scene=0)
    size = 256
    x, y = 2600, 2600  # level-1 pixels, inside tissue
    level0 = src.read_level(0, 2 * x, 2 * y, 2 * size, 2 * size).astype(np.float64)
    ref = level0.reshape(size, 2, size, 2, 3).mean(axis=(1, 3))
    level1 = src.read_level(1, x, y, size, size).astype(np.float64)
    r = 3
    errors = {
        (dx, dy): float(
            np.abs(level1[r + dy : size - r + dy, r + dx : size - r + dx] - ref[r:-r, r:-r]).mean()
        )
        for dx in range(-r, r + 1)
        for dy in range(-r, r + 1)
    }
    assert min(errors, key=errors.__getitem__) == (0, 0)


@pytest.mark.testdata
def test_attachments_are_never_read(jxr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(self: Any, *args: Any) -> Any:
        raise AssertionError("an attachment (label, slide preview, thumbnail) was read")

    for attr in ("attachments", "save_attachments"):
        monkeypatch.setattr(czifile.CziFile, attr, forbidden)
    monkeypatch.setattr(czifile.CziFile, "attachment_directory", property(forbidden))
    src = CziSource(jxr, name="s1", scene=0)
    for level, (w, h) in enumerate(src.level_dimensions):
        src.read_level(level, 0, 0, min(w, 2048), min(h, 2048))
