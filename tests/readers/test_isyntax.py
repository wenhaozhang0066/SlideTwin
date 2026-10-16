"""Philips iSyntax reader (M8): pyisyntax in a worker process per slide, the level offset rule, the
pixel size, the version gate and refusals. CI has no iSyntax sample, so a fake of the library layer
(tests/helpers/fake_isyntax.py) stands in for pyisyntax in real worker processes; local runs also
check real slides (data/testdata/isyntax/)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import isyntax
import numpy as np
import pytest

import slidetwin.readers.isyntax as reader
import slidetwin.readers.worker as shared
from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import FormatMatch, sniff
from slidetwin.core.geometry import LevelTransform
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers import open_source
from slidetwin.readers.isyntax import ISyntaxSource, interface_version, level_shifts, open_reader
from slidetwin.readers.worker import Worker
from tests.conftest import TESTDATA_DIR
from tests.helpers.fake_isyntax import BLOCK, CHUNK, header, pixels, write_slide

MATCH = FormatMatch("philips-isyntax", "isyntax")
FAKE = "tests.helpers.fake_isyntax"
SHORT = r"\(the slide file is shorter than its header says\)"


def write(tmp_path: Path, data: bytes, name: str = "slide.isyntax") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.fixture
def fake_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake pyisyntax in the workers."""
    monkeypatch.setattr(reader, "LIBRARY", FAKE)
    monkeypatch.setattr(reader, "LOWLEVEL", FAKE)


@pytest.fixture
def fake(fake_library: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Worker[Any]]]:
    """The fake pyisyntax in the workers. The list holds every worker the reader starts; each must
    have ended by the end of the test."""
    started: list[Worker[Any]] = []

    class Recorded(Worker[Any]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(reader, "Worker", Recorded)
    yield started
    assert all(w._process is None for w in started)  # every test ends the workers it starts


def open_fake(path: Path, mpp_override: Any = None, **spec: Any) -> ISyntaxSource:
    write_slide(path, **spec)
    return open_reader(path, MATCH, name="s1", mpp_override=mpp_override, options=ReaderOptions())


def process(src: ISyntaxSource) -> Any:
    assert src._worker._process is not None
    return src._worker._process


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


# --- the reader on the fake library, in worker processes ---------------------------------------


def test_reads_undo_the_level_offset_in_the_worker(tmp_path: Path, fake: list[Worker[Any]]) -> None:
    src = open_fake(tmp_path / "slide.isyntax")
    assert src.level_dimensions == ((320, 240), (160, 120), (80, 60), (40, 30))
    assert src.level_transforms == tuple(LevelTransform(2.0**k, 2.0**k) for k in range(4))
    assert src.mpp == (0.25, 0.25)
    for n, (w, h) in enumerate(src.level_dimensions):  # levels >= 2 are read one pixel early
        np.testing.assert_array_equal(src.read_level(n, 0, 0, w, h), pixels(n, 0, 0, w, h))
        np.testing.assert_array_equal(
            src.read_level(n, 3, 2, w - 3, h - 2), pixels(n, 3, 2, w - 3, h - 2)
        )
    with ThreadPoolExecutor(4) as pool:  # the server's threads; the fake crashes on a second one
        blocks = list(pool.map(lambda k: src.read_level(0, k, k, 64, 64), range(16)))
    for k, block in enumerate(blocks):
        np.testing.assert_array_equal(block, pixels(0, k, k, 64, 64))
    worker = process(src)
    src.close()
    assert worker.returncode == 0  # it left on request
    assert len(fake) == 1
    assert src.provenance == {
        "reader": "pyisyntax",
        "format": "philips-isyntax",
        "mpp_source": "isyntax UFS_IMAGE_DIMENSION_SCALE_FACTOR",
        "fill": [255, 255, 255],
        "level_shifts": [0, 0, 1, 1],
        "versions": {"pyisyntax": "0.1.7"},
    }


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize(
    "change", [{"known": False}, {"unit": "MilliMeter"}, {"unit": None}, {"mpp": [1e3, 1e3]}]
)
def test_pixel_size_is_used_only_when_known(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(SlideMetadataError, match="refusing to guess"):
        open_fake(tmp_path / "slide.isyntax", **change)
    src = open_fake(tmp_path / "slide.isyntax", mpp_override=(0.26, 0.26), **change)
    assert src.mpp == (0.26, 0.26)
    assert src.provenance["mpp_source"] == "mpp_override"
    src.close()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"model": 100}, "disagree on the iSyntax version"),
        ({"scales": [0, 1, 3, 4]}, "not a series of 2\\^n reductions"),
        ({"offsets": [0, 1, 4, 30]}, "level offsets that SlideTwin has not seen"),
        ({"dims": [[320, 240], [159, 120], [80, 60], [40, 30]]}, "not a series of 2\\^n"),
        ({"dims": [], "offsets": [], "late": [], "scales": []}, "not a series of 2\\^n"),
    ],
)
def test_refused_pyramids_end_the_worker(
    tmp_path: Path, fake: list[Worker[Any]], change: dict[str, Any], message: str
) -> None:
    with pytest.raises(SlideMetadataError, match=message):
        open_fake(tmp_path / "slide.isyntax", **change)
    assert len(fake) == 1  # and the fixture checks that it ended


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize("fail", ["open", "levels"])
def test_open_failures(tmp_path: Path, fail: str) -> None:
    with pytest.raises(SlideMetadataError, match=r"pyisyntax backend \(RuntimeError\)"):
        open_fake(tmp_path / "slide.isyntax", fail=fail)


@pytest.mark.usefixtures("fake")
def test_read_errors_name_only_the_slide(tmp_path: Path) -> None:
    src = open_fake(tmp_path / "slide.isyntax", fail="read")
    first = process(src).pid
    for _ in range(2):
        with pytest.raises(SlideReadError, match=r"'s1' could not be decoded at level 1") as e:
            src.read_level(1, 0, 0, 8, 8)
        assert "(OSError)" in str(e.value)
        assert "secret" not in str(e.value)
    assert process(src).pid == first  # a library error keeps the worker
    src.close()
    src.close()  # closing twice is harmless
    with pytest.raises(SlideReadError, match=r"\(the slide is closed\)"):
        src.read_level(1, 0, 0, 8, 8)


@pytest.mark.usefixtures("fake")
def test_blocks_other_than_rgba_are_refused(tmp_path: Path) -> None:
    src = open_fake(tmp_path / "slide.isyntax", fail="rgb")
    with pytest.raises(SlideReadError, match="other samples than RGBA"):
        src.read_level(0, 0, 0, 8, 8)
    src.close()


@pytest.mark.usefixtures("fake")
def test_a_crash_or_a_hang_fails_one_read_of_one_slide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r04c and r04e: libisyntax ran in the server, so a truncated copy crashed the whole
    server (SIGSEGV) or hung its one pyisyntax thread, and with it every iSyntax slide. Now each
    slide has its own worker. A crash fails one read, and the next read starts a new worker; a
    hang is stopped after the timeout, and other slides are read meanwhile."""
    good = open_fake(tmp_path / "good.isyntax")
    marker = tmp_path / "crash"
    marker.touch()
    crashing = open_fake(tmp_path / "crash.isyntax", fail="crash-once", marker=str(marker))
    first = process(crashing)
    with pytest.raises(SlideReadError, match=r"at level 0 \(the worker stopped\)"):
        crashing.read_level(0, 0, 0, 16, 16)
    assert first.returncode == -signal.SIGSEGV
    np.testing.assert_array_equal(crashing.read_level(0, 0, 0, 16, 16), pixels(0, 0, 0, 16, 16))
    assert process(crashing).pid != first.pid
    np.testing.assert_array_equal(good.read_level(0, 5, 5, 8, 8), pixels(0, 5, 5, 8, 8))
    crashing.close()

    monkeypatch.setattr(shared, "TIMEOUT_S", 3.0)
    monkeypatch.setattr(shared, "STOP_S", 0.2)
    hanging = open_fake(tmp_path / "hang.isyntax", fail="hang")
    with ThreadPoolExecutor(1) as pool:
        stuck = pool.submit(hanging.read_level, 0, 0, 0, 8, 8)
        time.sleep(0.5)  # the read that hangs is under way
        start = time.perf_counter()
        np.testing.assert_array_equal(good.read_level(1, 0, 0, 8, 8), pixels(1, 0, 0, 8, 8))
        assert time.perf_counter() - start < 1.0  # the other slide did not wait for it
        assert not stuck.done()
        with pytest.raises(SlideReadError, match=r"\(the worker took longer than 3 s\)"):
            stuck.result()
    hanging.close()
    good.close()


@pytest.mark.usefixtures("fake")
def test_a_copy_cut_short_fails_its_reads_at_once(tmp_path: Path) -> None:
    """Review r04: libisyntax reads past the end of a truncated copy and decodes what its buffer
    held, which crashed the process, looped forever or gave wrong pixels. A read that comes back
    short now ends the worker at once, and the read fails with that reason."""
    path = write_slide(tmp_path / "cut.isyntax")
    path.write_bytes(path.read_bytes()[:-1])  # the last byte of the block data is missing
    src = open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions())
    for _ in range(2):  # each read starts a new worker, which reads short again
        start = time.perf_counter()
        with pytest.raises(SlideReadError, match=SHORT):
            src.read_level(2, 0, 0, 8, 8)
        assert time.perf_counter() - start < 10  # no timeout
    src.close()


def test_a_copy_cut_in_its_seektable_is_refused_at_open(
    tmp_path: Path, fake: list[Worker[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy that ends in the data libisyntax reads when it opens the file (its seektable) is
    refused then, with the reason, and the worker is not started again: the cut stays."""
    path = write_slide(tmp_path / "cut.isyntax")
    path.write_bytes(path.read_bytes()[: -BLOCK - 1])  # the last seektable byte is missing too
    starts: list[Any] = []
    real = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        starts.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(SlideMetadataError, match=r"opened with the pyisyntax backend " + SHORT):
        open_reader(path, MATCH, name="s1", mpp_override=None, options=ReaderOptions())
    assert len(starts) == 1


@pytest.mark.usefixtures("fake_library")
def test_a_dropped_source_ends_its_worker(tmp_path: Path) -> None:
    src = open_fake(tmp_path / "slide.isyntax")
    worker = process(src)
    del src  # its socket closes with it, and the worker leaves
    assert worker.wait(10) == 0


@pytest.mark.usefixtures("fake")
def test_open_source_and_options(tmp_path: Path) -> None:
    path = write_slide(tmp_path / "slide.isyntax")
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


def test_damaged_reports_are_refused() -> None:
    good = {
        "dims": [[8, 6], [4, 3]],
        "scales": [0, 1],
        "offsets": [0, 1.0],
        "model": 5,
        "mpp": None,
    }
    assert reader.opened(good).dims == ((8, 6), (4, 3))
    for change in (
        {"extra": 1},
        {"dims": [[8, 6, 1], [4, 3]]},
        {"scales": [0]},
        {"offsets": [0, "1"]},
        {"model": -5},
        {"mpp": [0.25]},
    ):
        with pytest.raises(shared.WorkerError, match=shared.DAMAGED):
            reader.opened({**good, **change})
    with pytest.raises(shared.WorkerError, match=shared.DAMAGED):
        reader.opened(None)


# --- what runs in the worker, run here where coverage sees it ---------------------------------

OPTIONS = {"library": FAKE, "lowlevel": FAKE, "cache_tiles": reader.CACHE_TILES}


def test_the_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_header = "x" * 2 * CHUNK  # read in several chunks before its end
    path = write_slide(tmp_path / "slide.isyntax", mpp=[0.5, 0.5], note=long_header)
    handle, report = reader.open_slide(str(path), OPTIONS)
    assert report == {
        "dims": [[320, 240], [160, 120], [80, 60], [40, 30]],
        "scales": [0, 1, 2, 3],
        "offsets": [0.0, 1.0, 4.0, 10.0],
        "model": 5,
        "mpp": [0.5, 0.5],
    }
    assert handle.cache_size == reader.CACHE_TILES
    rgba = reader.read(handle, {"level": 2, "rect": [-1, -1, 5, 4]})  # libisyntax's coordinates
    np.testing.assert_array_equal(rgba[:, :, :3], pixels(2, 0, 0, 5, 4))
    handle.close()
    write_slide(path, fail="open")
    with pytest.raises(RuntimeError):
        reader.open_slide(str(path), OPTIONS)

    def end(status: int) -> None:  # in place of os._exit, which would end this process
        raise SystemExit(status)

    monkeypatch.setattr(os, "_exit", end)
    # Copies cut short: after the end of the header (whose last chunk may be short), a read that
    # comes back short ends the worker.
    write_slide(path)
    path.write_bytes(path.read_bytes()[:-1])  # in the codeblocks: the reads fail
    handle, _ = reader.open_slide(str(path), OPTIONS)
    with pytest.raises(SystemExit) as stopped:
        reader.read(handle, {"level": 0, "rect": [0, 0, 4, 4]})
    assert stopped.value.code == shared.TRUNCATED
    handle.close()
    path.write_bytes(path.read_bytes()[: -BLOCK - 1])  # in the seektable: the open fails
    with pytest.raises(SystemExit) as stopped:
        reader.open_slide(str(path), OPTIONS)
    assert stopped.value.code == shared.TRUNCATED


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


REAL = {"library": "isyntax", "lowlevel": "isyntax._pyisyntax", "cache_tiles": 100}


@pytest.mark.testdata
def test_bioimage_archive_slide(monkeypatch: pytest.MonkeyPatch) -> None:
    """IC-EN-00494-01 (BioImage Archive S-BIAD1199, CC BY 4.0): the survey's small H&E slide."""
    path = local_sample("IC-EN-00494-01.isyntax")

    def forbidden(*args: Any) -> Any:
        raise AssertionError("the label, the macro image or the barcode was read")

    # The adapter with the real library, here (on this thread only): it never asks for the label,
    # the macro image or the barcode.
    for attr in ("read_label_image_jpeg", "read_macro_image_jpeg"):
        monkeypatch.setattr(isyntax.ISyntax, attr, forbidden)
    monkeypatch.setattr(isyntax.ISyntax, "barcode", property(forbidden))
    handle, report = reader.open_slide(str(path), REAL)
    assert reader.read(handle, {"level": 0, "rect": [0, 0, 16, 16]}).shape == (16, 16, 4)
    handle.close()

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
    assert [list(d) for d in src.level_dimensions] == report["dims"]
    assert src.mpp == (0.25, 0.25)
    assert src.provenance["level_shifts"] == [0, 0, 1, 1, 1]
    # the survey's numbers: a tissue region and the glass in the corner, at level 0
    region = src.read_level(0, 5264, 5296, 512, 512)
    assert mean_rgb(region) == pytest.approx([203.31, 171.02, 203.23], abs=0.01)
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
    # a new worker serves the same pixels
    worker = process(src)
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait()
    np.testing.assert_array_equal(src.read_level(0, 5264, 5296, 512, 512), region)
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


@pytest.mark.testdata
def test_copies_of_a_real_slide_cut_short(tmp_path: Path) -> None:
    """Review r04 on a real slide: copies cut at 0.5-2% of their data crashed the process, and
    copies cut at 10-30% hung it. A copy cut in its seektable (here up to 2.7% of the data) is now
    refused at open, and every read of the others that needs a missing part fails at once, with
    the reason."""
    data = local_sample("IC-EN-00494-01.isyntax").read_bytes()
    start = data.index(b"\x04") + 1  # the end of the XML header
    for fraction in (0.005, 0.02, 0.1, 0.3, 0.7):
        path = write(tmp_path, data[: start + int((len(data) - start) * fraction)])
        if fraction < 0.027:
            with pytest.raises(SlideMetadataError, match=SHORT):
                ISyntaxSource(path, name="s1")
            continue
        src = ISyntaxSource(path, name="s1")
        reasons = []
        for level, (w, h) in enumerate(src.level_dimensions):
            for fx, fy in ((0.5, 0.5), (0.1, 0.9), (0.9, 0.1)):
                x, y = int((w - min(512, w)) * fx), int((h - min(512, h)) * fy)
                begin = time.perf_counter()
                try:
                    src.read_level(level, x, y, min(512, w), min(512, h))
                except SlideReadError as err:
                    reasons.append(err.message)
                assert time.perf_counter() - begin < 30
        src.close()
        assert reasons, fraction
        assert all("shorter than its header says" in r for r in reasons), reasons
