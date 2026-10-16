"""Philips iSyntax reader (M8): the one pyisyntax thread, the level offset rule, the pixel size,
the version gate and refusals. CI has no iSyntax sample, so a thin fake of the library layer stands
in for pyisyntax there; local runs also check real slides (data/testdata/isyntax/)."""

from __future__ import annotations

import gc
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import isyntax
import numpy as np
import pytest

import slidetwin.readers.isyntax as reader
from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.isyntax import ISyntaxSource, interface_version, level_shifts, open_reader
from tests.conftest import TESTDATA_DIR
from tests.helpers import fake_isyntax
from tests.helpers.fake_isyntax import Fake
from tests.helpers.synth import pyramid

MATCH = FormatMatch("philips-isyntax", "isyntax")
THREAD = "slidetwin-isyntax"


def header(version: str = "5.0", tail: bytes = b"") -> bytes:
    """The start of an iSyntax XML header, as Philips writes it."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\r\n<DataObject ObjectType="DPUfsImport">\r\n'
        b'\t<Attribute Name="PIM_DP_UFS_INTERFACE_VERSION" Group="0x301D" Element="0x1001" '
        b'PMSVR="IString">' + version.encode() + b"</Attribute>\r\n" + tail
    )


def write(tmp_path: Path, data: bytes, name: str = "slide.isyntax") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    """pyisyntax replaced by the fake: a 4-level pyramid of noise (a misplaced level shows)."""
    rng = np.random.default_rng(3)
    slide = Fake(pyramid(rng.integers(0, 256, (240, 320, 3), dtype=np.uint8), 4))
    fake_isyntax.ISyntax.current = slide
    monkeypatch.setattr(isyntax, "ISyntax", fake_isyntax.ISyntax)
    monkeypatch.setattr(reader, "lib", fake_isyntax.Lib())
    monkeypatch.setattr(reader, "ffi", fake_isyntax.Ffi())
    yield slide
    assert reader._HANDLES == {}  # every test closes what it opens


def open_fake(tmp_path: Path, **kw: Any) -> ISyntaxSource:
    path = write(tmp_path, header())
    return open_reader(
        path, MATCH, name="s1", mpp_override=kw.pop("mpp_override", None), options=ReaderOptions()
    )


# --- the interface version and the level offsets -----------------------------------------------


def test_interface_version(tmp_path: Path) -> None:
    assert interface_version(write(tmp_path, header("5.0")), "s1") == 5
    for version, message in (
        ("100.5", r"Philips iSyntax 2 \(interface version 100.5\)"),
        ("4.0", r"interface version 4.0; SlideTwin has checked version 5 only"),
    ):
        with pytest.raises(SlideMetadataError, match=message) as err:
            interface_version(write(tmp_path, header(version)), "s1")
        assert "Philips' software" in (err.value.hint or "")
    no_version = header().replace(b"INTERFACE_VERSION", b"OTHER")
    after_end = header("", b"\x04") + b"5.0</Attribute>"  # the header ends before the value
    for data in (no_version, after_end):
        with pytest.raises(SlideMetadataError, match="without an interface version"):
            interface_version(write(tmp_path, data), "s1")
    with pytest.raises(SlideMetadataError, match="opened with the pyisyntax backend"):
        interface_version(tmp_path / "missing.isyntax", "s1")


def test_level_shifts() -> None:
    measured = [0, 1, 4, 10, 22, 46, 94, 190, 382]  # libisyntax, levels 0-8 (survey)
    assert level_shifts(measured) == [0, 0, 1, 1, 1, 1, 1, 1, 1]
    assert level_shifts([0, 0, 0]) == [0, 0, 0]  # a libisyntax that aligns its levels itself
    for odd in ([], [1, 1], [0, float("nan")], [0, -1], [0, 1, 12]):
        assert level_shifts(odd) is None


# --- the reader on the fake library ----------------------------------------------------------


def test_reads_undo_the_level_offset_on_one_thread(tmp_path: Path, fake: Fake) -> None:
    src = open_fake(tmp_path)
    assert src.level_dimensions == ((320, 240), (160, 120), (80, 60), (40, 30))
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(4))
    assert src.mpp == (0.25, 0.25)
    for n, truth in enumerate(fake.levels):  # levels >= 2 are read one libisyntax pixel early
        h, w = truth.shape[:2]
        np.testing.assert_array_equal(src.read_level(n, 0, 0, w, h), truth)
        np.testing.assert_array_equal(src.read_level(n, 3, 2, w - 3, h - 2), truth[2:, 3:])
    with ThreadPoolExecutor(4) as pool:  # the server's threads
        list(pool.map(lambda k: src.read_level(0, k, k, 64, 64), range(16)))
    src.close()
    assert fake.threads == {f"{THREAD}_0"}  # open, reads and close: all on the pyisyntax thread
    assert fake.closed == 1
    prov = src.provenance
    assert prov == {
        "reader": "pyisyntax",
        "format": "philips-isyntax",
        "mpp_source": "isyntax UFS_IMAGE_DIMENSION_SCALE_FACTOR",
        "fill": [255, 255, 255],
        "level_shifts": [0, 0, 1, 1],
        "versions": {"pyisyntax": "0.1.7"},
    }


def test_pixel_size_is_used_only_when_known(tmp_path: Path, fake: Fake) -> None:
    for change in ({"known": False}, {"unit": b"MilliMeter"}, {"unit": None}, {"mpp": (1e3, 1e3)}):
        for key, value in change.items():
            setattr(fake, key, value)
        with pytest.raises(SlideMetadataError, match="refusing to guess"):
            open_fake(tmp_path)
        fake.known, fake.unit, fake.mpp = True, b"MicroMeter", (0.25, 0.25)
    fake.known = False
    src = open_fake(tmp_path, mpp_override=(0.26, 0.26))
    assert src.mpp == (0.26, 0.26)
    assert src.provenance["mpp_source"] == "mpp_override"
    src.close()
    assert fake.closed == 5  # the refused openings closed their handles too


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"model": 100}, "disagree on the iSyntax version"),
        ({"scales": [0, 1, 3, 4]}, "not a series of 2\\^n reductions"),
        ({"offsets": [0, 1, 4, 30]}, "level offsets that SlideTwin has not seen"),
    ],
)
def test_refused_pyramids_close_the_handle(
    tmp_path: Path, fake: Fake, change: dict[str, Any], message: str
) -> None:
    for key, value in change.items():
        setattr(fake, key, value)
    with pytest.raises(SlideMetadataError, match=message):
        open_fake(tmp_path)
    assert fake.closed == 1


def test_level_sizes_must_be_floor_halves(tmp_path: Path, fake: Fake) -> None:
    fake.levels[1] = fake.levels[1][:, :-1]
    with pytest.raises(SlideMetadataError, match="not a series of 2\\^n reductions"):
        open_fake(tmp_path)
    fake.levels = []
    with pytest.raises(SlideMetadataError, match="not a series of 2\\^n reductions"):
        open_fake(tmp_path)


def test_open_failures(tmp_path: Path, fake: Fake) -> None:
    fake.fail = "open"
    with pytest.raises(SlideMetadataError, match=r"pyisyntax backend \(RuntimeError\)"):
        open_fake(tmp_path)
    assert fake.files[-1].closed  # the file it was given
    fake.fail = "levels"
    with pytest.raises(SlideMetadataError, match=r"pyisyntax backend \(RuntimeError\)"):
        open_fake(tmp_path)
    assert fake.closed == 1


def test_read_errors_name_only_the_slide(tmp_path: Path, fake: Fake) -> None:
    src = open_fake(tmp_path)
    fake.fail = "read"
    with pytest.raises(SlideReadError, match=r"Slide 's1' could not be decoded at level 1") as err:
        src.read_level(1, 0, 0, 8, 8)
    assert "secret" not in str(err.value)
    src.close()
    src.close()  # closing twice is harmless
    assert fake.closed == 1
    with pytest.raises(SlideReadError, match=r"\(KeyError\)"):  # the handle is gone
        src.read_level(1, 0, 0, 8, 8)


def test_a_dropped_source_is_closed_on_the_pyisyntax_thread(tmp_path: Path, fake: Fake) -> None:
    src = open_fake(tmp_path)
    del src
    gc.collect()
    reader.on_isyntax_thread(lambda: None)  # the queued close ran before this
    assert fake.closed == 1
    assert fake.threads == {f"{THREAD}_0"}


def test_discard_during_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = ThreadPoolExecutor(1)
    stopped.shutdown()
    monkeypatch.setattr(reader, "_THREAD", stopped)
    reader._discard(12345)  # nothing can be queued; no error


def test_open_source_and_options(tmp_path: Path, fake: Fake) -> None:
    path = write(tmp_path, header())
    assert sniff(path) == MATCH
    src = open_source(path, MATCH, name="s1")
    assert isinstance(src, ISyntaxSource)
    src.close()
    with pytest.raises(SlideMetadataError, match="has no scene to choose"):
        open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions(scene=0))


def test_a_damaged_file_with_the_real_library(tmp_path: Path) -> None:
    path = write(tmp_path, header() + b"\x04" + bytes(range(256)) * 64)
    with pytest.raises(SlideMetadataError, match=r"opened with the pyisyntax backend \(\w+\)"):
        ISyntaxSource(path, name="s1")


# --- real slides (local runs only: no iSyntax sample is pinned for CI yet) ------------------------


def local_sample(name: str) -> Path:
    path = TESTDATA_DIR / "isyntax" / name
    if not path.is_file():
        pytest.skip(f"local iSyntax sample {name} not in {TESTDATA_DIR / 'isyntax'}")
    return path


def box(a: np.ndarray, f: int) -> np.ndarray:
    h, w = a.shape[0] // f, a.shape[1] // f
    return a[: h * f, : w * f].reshape(h, f, w, f, 3).astype(np.float64).mean(axis=(1, 3))


def mean_rgb(a: np.ndarray) -> list[float]:
    return a.reshape(-1, 3).mean(axis=0).tolist()  # type: ignore[no-any-return]


def assert_levels_aligned(src: ISyntaxSource, x0: int, y0: int, levels: range) -> None:
    """Level n against the 2^n box mean of level 0 around (x0, y0): no shift fits better."""
    size, r = 128, 3
    for n in levels:
        f = 2**n
        x, y = x0 // f - size // 2, y0 // f - size // 2
        ref = box(src.read_level(0, x * f, y * f, size * f, size * f), f)
        errors = {}
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                moved = src.read_level(n, x + dx, y + dy, size, size).astype(np.float64)
                errors[dx, dy] = float(np.abs(moved - ref).mean())
        assert min(errors, key=errors.__getitem__) == (0, 0), n


@pytest.mark.testdata
def test_bioimage_archive_slide(monkeypatch: pytest.MonkeyPatch) -> None:
    """IC-EN-00494-01 (BioImage Archive S-BIAD1199, CC BY 4.0): the survey's small H&E slide."""
    path = local_sample("IC-EN-00494-01.isyntax")

    def forbidden(*args: Any) -> Any:
        raise AssertionError("the label, the macro image or the barcode was read")

    for attr in ("read_label_image_jpeg", "read_macro_image_jpeg"):
        monkeypatch.setattr(isyntax.ISyntax, attr, forbidden)
    monkeypatch.setattr(isyntax.ISyntax, "barcode", property(forbidden))
    assert sniff(path) == MATCH
    src = open_source(path, MATCH, name="s1")
    assert isinstance(src, ISyntaxSource)
    assert src.level_dimensions == (
        (12102, 12102),
        (6051, 6051),
        (3025, 3025),
        (1512, 1512),
        (756, 756),
    )
    assert src.mpp == (0.25, 0.25)
    assert src.provenance["level_shifts"] == [0, 0, 1, 1, 1]
    # the survey's numbers: a tissue region and the glass in the corner, at level 0
    assert mean_rgb(src.read_level(0, 5264, 5296, 512, 512)) == pytest.approx(
        [203.31, 171.02, 203.23], abs=0.01
    )
    assert mean_rgb(src.read_level(0, 0, 0, 256, 256)) == pytest.approx(
        [226.91, 225.89, 228.74], abs=0.01
    )
    assert_levels_aligned(src, 5520, 5552, range(1, 5))
    names: set[str] = set()

    def read(k: int) -> None:
        names.add(threading.current_thread().name)
        src.read_level(0, (k * 997) % 11000, (k * 613) % 11000, 512, 512)

    with ThreadPoolExecutor(8) as pool:  # threads other than the first one crash libisyntax
        list(pool.map(read, range(32)))
    assert len(names) > 1
    src.close()


@pytest.mark.testdata
def test_openphi_test_slide() -> None:
    """testslide.isyntax (Zenodo 5037046, MIT): eight levels, IHC cores."""
    src = ISyntaxSource(local_sample("testslide.isyntax"), name="s1")
    assert src.level_dimensions[0] == (37382, 73222)
    assert src.level_dimensions[-1] == (292, 572)
    assert src.provenance["level_shifts"] == [0, 0, 1, 1, 1, 1, 1, 1]
    assert mean_rgb(src.read_level(0, 17920, 39168, 512, 512)) == pytest.approx(
        [170.77, 141.52, 131.8], abs=0.01
    )
    assert_levels_aligned(src, 18176, 39424, range(1, 6))
    src.close()
