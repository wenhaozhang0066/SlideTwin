"""Zeiss ZVI reader (M8): OLE2 files that are not ZVI, the reasons each ZVI is refused for now, the
ortho-view rule, and the OLE2 directory reader. CI uses synthetic OLE2 files and a fake slideio in
the worker; Zeiss-1-Merged (tier 3) checks the real library."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import slidetwin.readers.slideio_worker as worker
import slidetwin.readers.zvi as reader
from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.slideio_worker import SlideioWorker
from slidetwin.readers.zvi import ole2_entries, split, zvi_items
from tests.conftest import sample_path
from tests.helpers.fake_slideio import scene_spec, write_spec
from tests.helpers.make_ole2 import STORAGE, STREAM, ZVI, write_ole2, zvi_tree

MATCH = FormatMatch("ole2", "zvi")
FLUORESCENCE = {"channels": 3, "dtype": "int16", "levels": 1}  # as slideio reports Zeiss-1


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> list[SlideioWorker]:
    """The fake slideio in the worker; the list collects the workers that were closed."""
    monkeypatch.setattr(worker, "LIBRARY", "tests.helpers.fake_slideio")
    closed: list[SlideioWorker] = []
    real = SlideioWorker.close

    def close(self: SlideioWorker) -> None:
        closed.append(self)
        real(self)

    monkeypatch.setattr(SlideioWorker, "close", close)
    return closed


def write_zvi(
    path: Path, scenes: list[dict[str, Any]] | None = None, entries: dict[str, int] | None = None
) -> Path:
    """A synthetic ZVI directory, one raw image per channel unless `entries` says otherwise, and
    what the fake slideio finds in it."""
    scenes = [scene_spec(**FLUORESCENCE)] if scenes is None else scenes
    items = scenes[0]["channels"] if scenes else 1
    write_ole2(path, zvi_tree(items) if entries is None else entries)
    write_spec(path, scenes, driver="ZVI")
    return path


def refused(path: Path, **kw: Any) -> SlideMetadataError:
    with pytest.raises(SlideMetadataError) as err:
        reader.open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions(**kw))
    return err.value


# --- what each ZVI is refused for ----------------------------------------------------------------


@pytest.mark.usefixtures("fake")
def test_fluorescence_waits_for_the_channel_renderer(tmp_path: Path) -> None:
    path = write_zvi(tmp_path / "a.zvi")
    assert sniff(path) == MATCH
    with pytest.raises(SlideMetadataError, match="3 channel\\(s\\) of 16-bit samples") as err:
        open_source(path, MATCH, name="s1")
    assert "does not render channel data yet" in err.value.message
    assert "not supported yet" in (err.value.hint or "")
    gray = write_zvi(tmp_path / "b.zvi", [scene_spec(channels=1, dtype="uint16", levels=1)])
    with pytest.raises(SlideMetadataError, match="1 channel\\(s\\) of 16-bit samples"):
        open_source(gray, MATCH, name="s1")
    mixed = write_zvi(tmp_path / "c.zvi", [scene_spec(dtype="float32", levels=1)])
    with pytest.raises(SlideMetadataError, match="float32 samples"):
        open_source(mixed, MATCH, name="s1")


@pytest.mark.usefixtures("fake")
def test_brightfield_is_not_validated_yet(tmp_path: Path) -> None:
    error = refused(write_zvi(tmp_path / "a.zvi", [scene_spec(levels=1)]))
    assert "brightfield colour ZVI image; SlideTwin has not validated" in error.message


def test_ortho_view_exports(tmp_path: Path, fake: list[SlideioWorker], monkeypatch: Any) -> None:
    monkeypatch.setattr(reader, "BAND_ROWS", 7)  # several bands, the last one short
    ortho = scene_spec(size=(90, 61), layout="ortho", **FLUORESCENCE)
    error = refused(write_zvi(tmp_path / "a.zvi", [ortho]))
    assert "is an ortho-view export" in error.message
    assert "ortho view" in (error.hint or "")
    assert len(fake) == 1  # the worker was closed
    gray = scene_spec(size=(90, 61), layout="ortho", channels=1, dtype="uint16", levels=1)
    assert "ortho-view export" in refused(write_zvi(tmp_path / "b.zvi", [gray])).message


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize(
    ("scenes", "entries", "message"),
    [
        ([scene_spec(z=13, **FLUORESCENCE)], ZVI, r"z-stack or time series \(z 13, t 1\)"),
        ([scene_spec(t=2, **FLUORESCENCE)], ZVI, r"\(z 1, t 2\)"),
        ([], ZVI, "holds 0 images"),
        ([scene_spec(**FLUORESCENCE)] * 2, ZVI, "holds 2 images"),
        (
            [scene_spec(**FLUORESCENCE)],
            {**ZVI, "Image/Item(3)": STORAGE, "Image/Item(4)": STORAGE},
            r"ZVI mosaic or has an unexpected layout \(5 raw images, 3 channel\(s\)\)",
        ),
    ],
)
def test_layouts_not_validated(
    tmp_path: Path, scenes: list[dict[str, Any]], entries: dict[str, int], message: str
) -> None:
    with pytest.raises(SlideMetadataError, match=message):
        reader.open_reader(
            write_zvi(tmp_path / "a.zvi", scenes, entries),
            MATCH,
            name="s1",
            mpp_override=None,
            options=ReaderOptions(),
        )


def test_other_ole2_files_and_choices(tmp_path: Path) -> None:
    mds = tmp_path / "slide.mds"
    write_ole2(mds, {"DSI0": STORAGE, "DSI0/Property": STREAM, "MoticInfo": STREAM})
    assert sniff(mds) == MATCH
    error = refused(mds)
    assert "OLE2 compound file but not a Zeiss ZVI image" in error.message
    assert "Motic MDS/MDSX has no open reader" in (error.hint or "")
    stream_only = tmp_path / "b.zvi"
    write_ole2(stream_only, {"Image": STREAM})
    assert "not a Zeiss ZVI image" in refused(stream_only).message
    for choice in ({"scene": 0}, {"plane": 0}, {"channels": (0,)}):
        assert "to choose" in refused(write_zvi(tmp_path / "c.zvi"), **choice).message


def test_split() -> None:
    assert not split(np.array([0, 0, 1, 1, 1, 0], bool))  # a dark frame alone
    assert split(np.array([0, 1, 0, 1, 0], bool))  # a dark line between lit ones
    assert not split(np.zeros(4, bool))
    assert not split(np.ones(4, bool))


# --- the OLE2 directory ------------------------------------------------------------------------


@pytest.mark.parametrize("layout", [{}, {"sector_size": 4096}, {"fat_sectors": 110}])
def test_directories(tmp_path: Path, layout: dict[str, int]) -> None:
    path = tmp_path / "a.zvi"
    write_ole2(path, ZVI, **layout)
    assert ole2_entries(path, "s1") == ZVI
    assert zvi_items(path, "s1") == 3


def damage(path: Path, offset: int, data: bytes) -> None:
    raw = bytearray(path.read_bytes())
    raw[offset : offset + len(data)] = data
    path.write_bytes(bytes(raw))


DIR = 2 * 512  # the first directory sector (after the header and one FAT sector)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.write_bytes(p.read_bytes()[:300]),  # shorter than a header
        lambda p: damage(p, 0, b"\x00" * 8),  # not OLE2 (the router checks this too)
        lambda p: damage(p, 30, struct.pack("<H", 10)),  # unknown sector size
        lambda p: damage(p, 48, struct.pack("<I", 9999)),  # directory outside the file
        lambda p: damage(p, 512 + 4 * 1, struct.pack("<I", 1)),  # directory chain loops
        lambda p: damage(p, 44, struct.pack("<I", 0)),  # no sector table
        lambda p: damage(p, DIR + 66, b"\x01"),  # no root entry
        lambda p: damage(p, DIR + 76, struct.pack("<I", 99)),  # child outside the directory
        lambda p: damage(p, DIR + 128 + 72, struct.pack("<I", 1)),  # a sibling loop
        lambda p: damage(p, DIR + 128 + 64, struct.pack("<H", 66)),  # name too long
        lambda p: damage(p, DIR + 128 + 64, struct.pack("<H", 9)),  # odd name length
        lambda p: damage(p, DIR + 128, b"\x00\xd8"),  # a lone surrogate in a name
        lambda p: p.write_bytes(p.read_bytes()[: DIR + 64]),  # truncated directory
    ],
)
def test_damaged_directories(tmp_path: Path, change: Any) -> None:
    path = tmp_path / "a.zvi"
    write_ole2(path, ZVI)
    change(path)
    with pytest.raises(SlideMetadataError, match="has an OLE2 directory that cannot be read"):
        ole2_entries(path, "s1")


# --- Zeiss-1-Merged (OpenSlide test data, "distributable", tier 3) -------------------------------


@pytest.mark.testdata
def test_zeiss_1_merged_is_an_ortho_view_export() -> None:
    path = sample_path("Zeiss/Zeiss-1-Merged.zvi", tier=3)
    assert sniff(path) == MATCH
    assert zvi_items(path, "s1") == 3
    w = SlideioWorker(path, "ZVI", "s1")
    try:
        (info,) = w.scenes  # the survey's numbers
        assert (info.size, info.channels, info.dtypes) == ((1480, 1132), 3, ("int16",) * 3)
        assert (info.z, info.t, info.levels) == (1, 1, ((1480, 1132),))
        assert info.resolution == pytest.approx((6.45e-8, 6.45e-8))
        block = w.read(0, 0, 0, 0, 1480, 1132).view(np.uint16)  # unsigned 14-bit samples
        assert block.max() == 16383
        rows = (block != 0).any(axis=(1, 2))  # the survey: an 18 px frame, a 50 px XZ strip
        assert rows.tolist() == [False] * 18 + [True] * 50 + [False] * 6 + rows[74:].tolist()
        assert reader.has_panels(w, info)
    finally:
        w.close()
    with pytest.raises(SlideMetadataError, match="is an ortho-view export"):
        open_source(path, MATCH, name="s1")
