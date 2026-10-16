"""slideio worker (M8): slideio's adapter in real worker processes, with crashes, timeouts and
restarts, and the scene reports a worker sends.

A fake slideio (tests/helpers/fake_slideio.py) runs in real worker processes, so CI checks the
process handling without a VSI or ZVI sample. The worker's own loop is also run in a thread here,
where coverage sees it. The protocol itself is tested in test_worker.py.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import slidetwin.readers.slideio_worker as worker
import slidetwin.readers.worker as shared
from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.readers.slideio_worker import SceneInfo, SlideioWorker, scene_infos
from slidetwin.readers.worker import (
    DAMAGED,
    WorkerError,
    receive,
    request_block,
    send,
    serve,
)
from tests.helpers.fake_slideio import LOG_LEVELS, pixels, scene_spec, write_spec

FAKE = "tests.helpers.fake_slideio"
ADAPTER = "slidetwin.readers.slideio_worker"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "LIBRARY", FAKE)


def slide(tmp_path: Path, scenes: list[dict[str, Any]] | None = None, **behaviour: Any) -> Path:
    path = tmp_path / "slide.bin"
    path.write_bytes(b"")
    write_spec(path, [scene_spec()] if scenes is None else scenes, **behaviour)
    return path


def pid(w: SlideioWorker) -> int:
    assert w._process is not None
    return w._process.pid


INFO = SceneInfo(
    size=(1000, 600),
    channels=3,
    dtypes=("uint8", "uint8", "uint8"),
    z=1,
    t=1,
    resolution=(0.5e-6, 0.5e-6),
    compression="Jpeg2000",
    levels=((1000, 600), (500, 300), (250, 150)),
)


# --- real worker processes running the fake library --------------------------------------------


@pytest.mark.usefixtures("fake")
def test_open_read_and_close(tmp_path: Path) -> None:
    w = SlideioWorker(slide(tmp_path), "VSI", "s1")
    assert w.scenes == [INFO]
    np.testing.assert_array_equal(w.read(0, 1, 10, 20, 30, 40), pixels(0, 1, 10, 20, 30, 40))
    process = w._process
    assert process is not None
    w.close()
    assert process.returncode == 0  # it left on request
    with pytest.raises(SlideReadError, match=r"at level 0 \(the slide is closed\)"):
        w.read(0, 0, 0, 0, 8, 8)
    w.close()  # closing twice is harmless


@pytest.mark.usefixtures("fake")
def test_gray_and_16_bit_blocks(tmp_path: Path) -> None:
    scenes = [scene_spec(channels=1), scene_spec(dtype="int16", levels=1)]
    w = SlideioWorker(slide(tmp_path, scenes), "ZVI", "s1")
    gray = w.read(0, 0, 5, 5, 7, 3)
    assert gray.shape == (3, 7)
    np.testing.assert_array_equal(gray, pixels(0, 0, 5, 5, 7, 3, channels=1))
    wide = w.read(1, 0, 0, 0, 4, 4)
    assert wide.dtype == np.int16
    np.testing.assert_array_equal(wide, pixels(1, 0, 0, 0, 4, 4, dtype="int16"))
    w.close()


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize(
    ("behaviour", "reason"),
    [
        ("error", r"\(RuntimeError\)"),
        ("crash", r"\(the worker stopped\)"),
    ],
)
def test_failed_opens(tmp_path: Path, behaviour: str, reason: str) -> None:
    with pytest.raises(SlideMetadataError, match=f"Slide 's1' could not be opened .*{reason}") as e:
        SlideioWorker(slide(tmp_path, open=behaviour), "VSI", "s1")
    assert "secret" not in str(e.value)  # library messages, which name files, stay in the worker


def count_starts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The pids of the workers started from now on."""
    pids: list[int] = []
    real = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real(*args, **kwargs)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    return pids


@pytest.mark.usefixtures("fake")
def test_a_worker_that_stops_while_opening_is_started_once_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r16: a worker that died while opening the slide (here it exits; in r16 it was killed
    from outside) was reported as damage, though the next attempt worked. A worker that stops
    before it answers is started once more; only a second stop is reported."""
    starts = count_starts(monkeypatch)
    marker = tmp_path / "crash"
    marker.touch()
    w = SlideioWorker(slide(tmp_path, open="crash-once", marker=str(marker)), "VSI", "s1")
    assert len(starts) == 2
    assert not marker.exists()  # the first worker stopped there
    assert w.scenes == [INFO]
    np.testing.assert_array_equal(w.read(0, 0, 1, 2, 3, 4), pixels(0, 0, 1, 2, 3, 4))
    w.close()
    for behaviour, tries in (("crash", 2), ("error", 1)):  # a library error is not tried again
        starts.clear()
        with pytest.raises(SlideMetadataError, match="could not be opened"):
            SlideioWorker(slide(tmp_path, open=behaviour), "VSI", "s1")
        assert len(starts) == tries


@pytest.mark.usefixtures("fake")
def test_an_open_that_hangs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "TIMEOUT_S", 3.0)
    monkeypatch.setattr(shared, "STOP_S", 0.2)
    with pytest.raises(SlideMetadataError, match=r"took longer than 3 s"):
        SlideioWorker(slide(tmp_path, open="hang"), "VSI", "s1")


@pytest.mark.usefixtures("fake")
def test_a_crash_fails_one_read_and_the_next_read_starts_a_new_worker(tmp_path: Path) -> None:
    marker = tmp_path / "crash"
    w = SlideioWorker(slide(tmp_path, read="crash-once", marker=str(marker)), "VSI", "s1")
    first = pid(w)
    marker.touch()
    with pytest.raises(SlideReadError, match=r"at level 2 \(the worker stopped\)"):
        w.read(0, 2, 0, 0, 16, 16)
    assert w._process is None
    np.testing.assert_array_equal(w.read(0, 2, 0, 0, 16, 16), pixels(0, 2, 0, 0, 16, 16))
    assert pid(w) != first
    w.close()


@pytest.mark.usefixtures("fake")
def test_a_worker_gone_between_reads_is_replaced(tmp_path: Path) -> None:
    w = SlideioWorker(slide(tmp_path), "VSI", "s1")
    first = pid(w)
    os.kill(first, signal.SIGKILL)
    assert w._process is not None
    w._process.wait()
    np.testing.assert_array_equal(w.read(0, 0, 1, 2, 3, 4), pixels(0, 0, 1, 2, 3, 4))
    assert pid(w) != first
    w.close()


@pytest.mark.usefixtures("fake")
def test_a_slide_that_changed_is_refused(tmp_path: Path) -> None:
    path = slide(tmp_path)
    w = SlideioWorker(path, "VSI", "s1")
    os.kill(pid(w), signal.SIGKILL)
    assert w._process is not None
    w._process.wait()
    write_spec(path, [scene_spec(size=(1200, 600))])
    with pytest.raises(SlideReadError, match="changed on disk since it was opened"):
        w.read(0, 0, 0, 0, 8, 8)
    assert w._process is None
    w.close()


@pytest.mark.usefixtures("fake")
def test_a_read_that_hangs_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = SlideioWorker(slide(tmp_path, read="hang"), "VSI", "s1")
    process = w._process
    monkeypatch.setattr(shared, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(shared, "STOP_S", 0.2)
    with pytest.raises(SlideReadError, match=r"took longer than 1 s"):
        w.read(0, 0, 0, 0, 8, 8)
    assert process is not None
    assert process.returncode == -signal.SIGKILL  # it did not leave when asked; it was killed
    w.close()


@pytest.mark.usefixtures("fake")
def test_library_errors_keep_the_worker(tmp_path: Path) -> None:
    w = SlideioWorker(slide(tmp_path, read="error"), "VSI", "s1")
    first = pid(w)
    for _ in range(2):
        with pytest.raises(SlideReadError, match=r"\(RuntimeError\)") as err:
            w.read(0, 0, 0, 0, 8, 8)
        assert "secret" not in str(err.value)
    assert pid(w) == first
    w.close()


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize("behaviour", ["shape", "dtype"])
def test_blocks_other_than_announced_stop_the_worker(tmp_path: Path, behaviour: str) -> None:
    w = SlideioWorker(slide(tmp_path, read=behaviour), "VSI", "s1")
    with pytest.raises(SlideReadError, match=DAMAGED):
        w.read(0, 0, 0, 0, 8, 8)
    assert w._process is None
    w.close()


@pytest.mark.usefixtures("fake")
def test_start_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_process(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fork failed")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    with pytest.raises(SlideMetadataError, match=r"no worker could start \(OSError\)"):
        SlideioWorker(slide(tmp_path), "VSI", "s1")


@pytest.mark.usefixtures("fake")
def test_a_worker_that_cannot_be_told_what_to_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn: Connection, message: Any) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(shared, "send", broken)
    with pytest.raises(SlideMetadataError, match=r"\(the worker stopped\)"):
        SlideioWorker(slide(tmp_path), "VSI", "s1")


def good_scene(**change: Any) -> dict[str, Any]:
    item = {
        "size": [1000, 600],
        "channels": 3,
        "dtypes": ["uint8"] * 3,
        "z": 1,
        "t": 1,
        "resolution": [5e-07, 5e-07],
        "compression": "Jpeg2000",
        "levels": [[1000, 600], [500, 300], [250, 150]],
    }
    item.update(change)
    return item


@pytest.mark.parametrize(
    "change",
    [
        {"name": "label"},
        {"channels": 2},
        {"channels": True},
        {"z": -1},
        {"size": [1000]},
        {"dtypes": ["uint8", "uint8", "un signed"]},
        {"resolution": ["5e-07", 5e-07]},
        {"compression": ""},
        {"levels": [[1000, 600, 1]]},
        {"levels": 3},
    ],
)
def test_scene_reports(change: dict[str, Any]) -> None:
    assert scene_infos([good_scene()]) == [INFO]
    with pytest.raises(WorkerError, match=DAMAGED):
        scene_infos([good_scene(**change)])
    for raw in (None, {"size": 1}, ["scene"], [[]]):
        with pytest.raises(WorkerError, match=DAMAGED):
            scene_infos(raw)


# --- the worker's own loop, in a thread ----------------------------------------------------------


def run_serve(path: Path, driver: str = "VSI") -> tuple[Connection, threading.Thread]:
    ours, theirs = Pipe()
    options = {"driver": driver, "library": FAKE}
    thread = threading.Thread(target=serve, args=(theirs, ADAPTER, str(path), options))
    thread.start()
    return ours, thread


def test_serve(tmp_path: Path) -> None:
    LOG_LEVELS.clear()
    ours, thread = run_serve(slide(tmp_path, [scene_spec(), scene_spec(channels=1)]))
    assert scene_infos(receive(ours, 5.0))[1].channels == 1
    assert LOG_LEVELS == ["OFF"]  # slideio's log names files
    request = {"op": "read", "scene": 1, "level": 1, "rect": [3, 4, 5, 6]}
    block = request_block(ours, request, (6, 5))
    np.testing.assert_array_equal(block, pixels(1, 1, 3, 4, 5, 6, channels=1))
    send(ours, {"op": "read", "scene": 7, "level": 0, "rect": [0, 0, 1, 1]})  # no such scene
    with pytest.raises(WorkerError, match="IndexError") as err:
        receive(ours, 5.0)
    assert err.value.reported
    send(ours, {"op": "close"})
    thread.join(5.0)
    assert not thread.is_alive()
    ours.close()


def test_serve_open_errors_and_a_closed_pipe(tmp_path: Path) -> None:
    ours, thread = run_serve(slide(tmp_path, open="error"))
    with pytest.raises(WorkerError, match="RuntimeError"):
        receive(ours, 5.0)
    thread.join(5.0)
    ours.close()
    ours, thread = run_serve(slide(tmp_path), driver="ZVI")  # the fake checks no driver here
    receive(ours, 5.0)
    ours.close()  # the server went away: the loop ends
    thread.join(5.0)
    assert not thread.is_alive()
    ours, thread = run_serve(tmp_path / "missing.bin")
    with pytest.raises(WorkerError, match="FileNotFoundError"):
        receive(ours, 5.0)
    thread.join(5.0)
    ours.close()
