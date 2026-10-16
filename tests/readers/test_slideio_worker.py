"""slideio worker (M8): the process boundary, the protocol, crashes, timeouts and restarts.

A fake slideio (tests/helpers/fake_slideio.py) runs in real worker processes, so CI checks the
process handling without a VSI or ZVI sample. The worker's own loop is also run in a thread here,
where coverage sees it.
"""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import threading
from collections.abc import Iterator
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import slidetwin.readers.slideio_worker as worker
from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.readers.slideio_worker import (
    DAMAGED,
    SceneInfo,
    SlideioWorker,
    WorkerError,
    block_header,
    library_version,
    receive,
    request_block,
    scene_infos,
    send,
    serve,
)
from tests.helpers.fake_slideio import LOG_LEVELS, pixels, scene_spec, write_spec

FAKE = "tests.helpers.fake_slideio"


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
        ("crash", r"\(the slideio worker stopped\)"),
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
    monkeypatch.setattr(worker, "TIMEOUT_S", 3.0)
    monkeypatch.setattr(worker, "STOP_S", 0.2)
    with pytest.raises(SlideMetadataError, match=r"took longer than 3 s"):
        SlideioWorker(slide(tmp_path, open="hang"), "VSI", "s1")


@pytest.mark.usefixtures("fake")
def test_a_crash_fails_one_read_and_the_next_read_starts_a_new_worker(tmp_path: Path) -> None:
    marker = tmp_path / "crash"
    w = SlideioWorker(slide(tmp_path, read="crash-once", marker=str(marker)), "VSI", "s1")
    first = pid(w)
    marker.touch()
    with pytest.raises(SlideReadError, match=r"at level 2 \(the slideio worker stopped\)"):
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
    monkeypatch.setattr(worker, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(worker, "STOP_S", 0.2)
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

    monkeypatch.setattr(worker, "send", broken)
    with pytest.raises(SlideMetadataError, match=r"\(the slideio worker stopped\)"):
        SlideioWorker(slide(tmp_path), "VSI", "s1")


def test_a_missing_library() -> None:
    assert library_version() == "2.10.0"
    with pytest.raises(ModuleNotFoundError) as err:
        library_version("slidetwin_no_such_library")
    assert err.value.name == "slidetwin_no_such_library"


# --- the protocol, with the test as the other end -----------------------------------------------


@pytest.fixture
def pipe() -> Iterator[tuple[Connection, Connection]]:
    ours, theirs = Pipe()
    yield ours, theirs
    ours.close()
    theirs.close()


@pytest.mark.parametrize(
    "data",
    [b"not json", b"[" * 100_000, b'["ok"]', b'{"ok": 1, "error": "X"}', b'{"done": 1}'],
)
def test_damaged_messages(pipe: tuple[Connection, Connection], data: bytes) -> None:
    ours, theirs = pipe
    theirs.send_bytes(data)
    with pytest.raises(WorkerError, match=DAMAGED) as err:
        receive(ours, 1.0)
    assert not err.value.reported
    assert not err.value.stopped


def test_reported_errors(pipe: tuple[Connection, Connection]) -> None:
    ours, theirs = pipe
    for error, shown in (("ValueError", "ValueError"), ("no such file /x", "an unknown error")):
        send(theirs, {"error": error})
        with pytest.raises(WorkerError, match=shown) as err:
            receive(ours, 1.0)
        assert err.value.reported
    send(theirs, {"error": 7})
    with pytest.raises(WorkerError, match="an unknown error"):
        receive(ours, 1.0)


def test_silent_stopped_and_oversized(pipe: tuple[Connection, Connection]) -> None:
    ours, theirs = pipe
    with pytest.raises(WorkerError, match=r"took longer than 0\.1 s") as err:
        receive(ours, 0.1)
    assert not err.value.stopped  # a worker that hangs is not started again at once
    # only the length header of an oversized message: sending the body would block once the
    # pipe's buffer is full (about 200 KB on CI runners), as nothing reads it
    os.write(theirs.fileno(), struct.pack("!i", worker.MESSAGE_MAX + 1))
    with pytest.raises(WorkerError, match=DAMAGED):
        receive(ours, 1.0)
    theirs.close()
    other, end = Pipe()
    end.close()
    with pytest.raises(WorkerError, match="stopped") as err:
        receive(other, 1.0)
    assert err.value.stopped
    other.close()
    other, end = Pipe()
    send(other, {"op": "read"})
    end.close()  # it died with the request unread: the connection is reset
    with pytest.raises(WorkerError, match="stopped") as err:
        receive(other, 1.0)
    assert err.value.stopped
    other.close()


@pytest.mark.parametrize(
    "header",
    [
        None,
        [],
        {"dtype": "float64", "shape": [2, 3, 3]},
        {"dtype": "uint8", "shape": [3, 2, 3]},
        {"dtype": "uint8", "shape": [2, 3, 0]},
        {"dtype": "uint8", "shape": [2, 3, 3, 1]},
        {"dtype": "uint8", "shape": [2.0, 3, 3]},
        {"dtype": ["uint8"], "shape": [2, 3, 3]},
        {"dtype": "uint8", "shape": 6},
        {"dtype": "uint8"},
    ],
)
def test_block_headers(header: Any) -> None:
    assert block_header({"dtype": "uint8", "shape": [2, 3, 3]}, (2, 3)) == ("uint8", (2, 3, 3))
    assert block_header({"dtype": "int16", "shape": [2, 3]}, (2, 3)) == ("int16", (2, 3))
    with pytest.raises(WorkerError, match=DAMAGED):
        block_header(header, (2, 3))


def test_a_block_shorter_than_announced(pipe: tuple[Connection, Connection]) -> None:
    ours, theirs = pipe
    send(theirs, {"ok": {"dtype": "uint8", "shape": [2, 2, 3]}})
    theirs.send_bytes(b"\x00" * 11)
    with pytest.raises(WorkerError, match=DAMAGED):
        request_block(ours, {"op": "read"}, (2, 2))
    assert json.loads(theirs.recv_bytes()) == {"op": "read"}
    ours.close()
    with pytest.raises(WorkerError, match="stopped"):
        request_block(ours, {"op": "read"}, (2, 2))


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
    thread = threading.Thread(target=serve, args=(theirs, str(path), driver, FAKE))
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
