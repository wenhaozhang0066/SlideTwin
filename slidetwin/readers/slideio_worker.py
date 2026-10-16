"""slideio in a worker process (M8), for Olympus VSI and Zeiss ZVI.

slideio crashed the whole process on several formats in the survey (SIGSEGV, SIGFPE, glibc
aborts), and Slide.get_scene_by_name() segfaults. So slideio never runs in the server: each open
slide has a worker, a fresh Python process that opens it once and answers read requests over a
socket. A crash fails the request in flight with SlideReadError; the next request starts a new
worker, which must find the same slide as before. Reads are deterministic, so a new worker serves
the same pixels.

The worker is started with a small bootstrap (python -I -c), not with multiprocessing's spawn: spawn
runs the server's main module again in the child, which breaks scripts without a main guard. The
file to open travels over the socket, so it never shows in the process list. The socket carries
JSON messages and raw pixel bytes, never pickles: a worker that a crafted file has taken over cannot
run code in the server, and every message is checked. Scenes are addressed by index only. Names,
raw metadata and auxiliary images (label, overview, macro) are never asked for, so they never leave
the worker.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import math
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass, fields
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.errors import SlideMetadataError, SlideReadError

LIBRARY = "slideio"
TIMEOUT_S = 120.0  # for an open or a read; a worker that takes longer is stopped
STOP_S = 2.0  # how long a worker may take to exit once its socket is closed
MESSAGE_MAX = 1 << 20  # bytes of one JSON message
DTYPES = frozenset({"uint8", "int16", "uint16"})  # sample types a worker may send
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")  # an exception type, a codec or a dtype
# The worker's first lines: take the server's import path, then serve. Isolated mode (-I) keeps
# the current folder and the environment out of the imports made before that.
_BOOT = (
    "import json, sys\n"
    "from multiprocessing.connection import Connection\n"
    "conn = Connection(int(sys.argv[1]))\n"
    "start = json.loads(conn.recv_bytes())\n"
    "sys.path[:] = start['path']\n"
    "from slidetwin.readers.slideio_worker import serve\n"
    "serve(conn, start['file'], start['driver'], start['library'])\n"
)


def library_version(library: str = LIBRARY) -> str:
    """The library's version. It is not imported here, only in workers; a missing library raises
    ModuleNotFoundError, which open_source reports with the extra to install."""
    if importlib.util.find_spec(library) is None:
        raise ModuleNotFoundError(f"No module named '{library}'", name=library)
    return importlib.metadata.version(library)


VERSION = library_version()


class WorkerError(Exception):
    """A request failed. `reported` is true when the library raised (the worker is still fine);
    otherwise the worker stopped, took too long or broke the protocol."""

    def __init__(self, reason: str, *, reported: bool = False) -> None:
        super().__init__(reason)
        self.reported = reported


STOPPED = "the slideio worker stopped"
DAMAGED = "the slideio worker sent a damaged answer"


@dataclass(frozen=True, slots=True)
class SceneInfo:
    """What a worker says about one scene: numbers and type names, no names from the file."""

    size: tuple[int, int]
    channels: int
    dtypes: tuple[str, ...]
    z: int
    t: int
    resolution: tuple[float, float]  # metres per pixel; 0 when the file has none
    compression: str
    levels: tuple[tuple[int, int], ...]


_FIELDS = frozenset(f.name for f in fields(SceneInfo))


class SlideioWorker:
    """One slide, opened by slideio in its own process."""

    def __init__(self, path: Path, driver: str, name: str) -> None:
        self._start_message = {
            "path": [p for p in sys.path if isinstance(p, str)],
            "file": str(path),
            "driver": driver,
            "library": LIBRARY,
        }
        self._name = name
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._conn: Connection | None = None
        self._closed = False
        try:
            self.scenes = self._start()
        except WorkerError as err:
            raise SlideMetadataError(
                f"Slide '{name}' could not be opened with the slideio backend ({err}).",
                hint="The slide's files may be damaged or incompletely copied.",
            ) from None

    def read(self, scene: int, level: int, x: int, y: int, w: int, h: int) -> NDArray[Any]:
        """Pixels of level `level` of scene `scene`, (h, w) or (h, w, channels), as slideio
        returns them. After a crash, a timeout or a broken message the worker is stopped, and the
        next read starts a new one; so does a read that finds its worker gone."""
        request = {"op": "read", "scene": scene, "level": level, "rect": [x, y, w, h]}
        with self._lock:
            try:
                if self._closed:
                    raise WorkerError("the slide is closed")
                if self._process is not None and self._process.poll() is not None:
                    self._stop()  # it ended between reads
                if self._conn is None and self._start() != self.scenes:
                    self._stop()
                    raise WorkerError("the slide changed on disk since it was opened")
                assert self._conn is not None
                return request_block(self._conn, request, (h, w))
            except WorkerError as err:
                if not err.reported:
                    self._stop()
                raise SlideReadError(
                    f"Slide '{self._name}' could not be decoded at level {level} ({err}).",
                    hint="The slide's files may be damaged; the next read starts a new worker.",
                ) from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                with contextlib.suppress(OSError):
                    send(self._conn, {"op": "close"})
            self._stop()

    def _start(self) -> list[SceneInfo]:
        ours, theirs = socket.socketpair()
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", _BOOT, str(theirs.fileno())],
                pass_fds=(theirs.fileno(),),
                stdin=subprocess.DEVNULL,
            )
        except OSError as err:  # no more processes or memory
            ours.close()
            raise WorkerError(f"no worker could start ({type(err).__name__})") from None
        finally:
            theirs.close()  # the worker has its own copy; a crash then reads as end of file
        self._process, self._conn = process, Connection(ours.detach())
        try:
            send(self._conn, self._start_message)
            return scene_infos(receive(self._conn, TIMEOUT_S))
        except OSError:
            self._stop()
            raise WorkerError(STOPPED) from None
        except WorkerError:
            self._stop()
            raise

    def _stop(self) -> None:
        """End the worker: it exits once its socket is closed; one that hangs is killed."""
        conn, process = self._conn, self._process
        self._conn = self._process = None
        if conn is not None:
            conn.close()
        if process is not None:
            try:
                process.wait(STOP_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


# --- the protocol ------------------------------------------------------------------------------


def send(conn: Connection, message: Any) -> None:
    conn.send_bytes(json.dumps(message).encode("ascii"))


def request_block(conn: Connection, request: dict[str, Any], hw: tuple[int, int]) -> NDArray[Any]:
    """Send a read request; the block, as announced by its header and of exactly that size."""
    try:
        send(conn, request)
    except OSError:
        raise WorkerError(STOPPED) from None
    dtype, shape = block_header(receive(conn, TIMEOUT_S), hw)
    size = math.prod(shape) * np.dtype(dtype).itemsize
    data = receive_bytes(conn, size, TIMEOUT_S)
    if len(data) != size:
        raise WorkerError(DAMAGED)
    return np.frombuffer(data, dtype).reshape(shape)


def receive(conn: Connection, timeout: float) -> Any:
    """The `ok` part of the next message. WorkerError for a reported error, a stopped or silent
    worker, or a message out of protocol."""
    data = receive_bytes(conn, MESSAGE_MAX, timeout)
    try:
        message = json.loads(data)
    except (ValueError, RecursionError):
        raise WorkerError(DAMAGED) from None
    if isinstance(message, dict) and set(message) == {"error"}:
        error = message["error"]
        valid = isinstance(error, str) and _NAME.fullmatch(error)
        raise WorkerError(error if valid else "an unknown error", reported=True)
    if not (isinstance(message, dict) and set(message) == {"ok"}):
        raise WorkerError(DAMAGED)
    return message["ok"]


def receive_bytes(conn: Connection, limit: int, timeout: float) -> bytes:
    if not conn.poll(timeout):
        raise WorkerError(f"the slideio worker took longer than {timeout:g} s")
    try:
        return conn.recv_bytes(limit)
    except (EOFError, ConnectionError):  # it ended, also while a request was on its way
        raise WorkerError(STOPPED) from None
    except OSError:  # longer than the limit
        raise WorkerError(DAMAGED) from None


def block_header(header: Any, hw: tuple[int, int]) -> tuple[str, tuple[int, ...]]:
    """The dtype and shape a worker announces for a read: the size asked for, a known type."""
    try:
        dtype, shape = header["dtype"], tuple(header["shape"])
        ok = dtype in DTYPES and all(type(v) is int for v in shape) and shape[:2] == hw
        ok = ok and (len(shape) == 2 or (len(shape) == 3 and 1 <= shape[2] <= 64))
    except (TypeError, KeyError):
        ok = False
    if not ok:
        raise WorkerError(DAMAGED)
    return dtype, shape


def scene_infos(raw: Any) -> list[SceneInfo]:
    """The scenes a worker reports, checked field by field."""
    try:
        return [_scene(item) for item in raw]
    except (TypeError, KeyError, ValueError):
        raise WorkerError(DAMAGED) from None


def _scene(item: dict[str, Any]) -> SceneInfo:
    if set(item) != _FIELDS:
        raise ValueError("unexpected fields")
    channels, z, t = _ints([item["channels"], item["z"], item["t"]], 3)
    dtypes = _names(item["dtypes"])
    if len(dtypes) != channels:
        raise ValueError("one sample type per channel")
    w, h = _ints(item["size"], 2)
    x_res, y_res = _floats(item["resolution"], 2)
    return SceneInfo(
        size=(w, h),
        channels=channels,
        dtypes=dtypes,
        z=z,
        t=t,
        resolution=(x_res, y_res),
        compression=_names([item["compression"]])[0],
        levels=tuple((lw, lh) for lw, lh in (_ints(level, 2) for level in item["levels"])),
    )


def _ints(values: Sequence[Any], n: int) -> tuple[int, ...]:
    if len(values) != n or not all(type(v) is int and v >= 0 for v in values):
        raise ValueError("expected non-negative integers")
    return tuple(values)


def _floats(values: Sequence[Any], n: int) -> tuple[float, ...]:
    if len(values) != n or not all(type(v) in (int, float) for v in values):
        raise ValueError("expected numbers")
    return tuple(float(v) for v in values)


def _names(values: Sequence[Any]) -> tuple[str, ...]:
    if not all(isinstance(v, str) and _NAME.fullmatch(v) for v in values):
        raise ValueError("expected type names")
    return tuple(values)


# --- the worker process --------------------------------------------------------------------------


def serve(conn: Connection, path: str, driver: str, library: str) -> None:
    """The worker: open the slide, report its scenes, then answer reads until the server asks it
    to stop or closes the socket."""
    with contextlib.suppress(EOFError, OSError):  # the server closed the socket
        try:
            module = importlib.import_module(library)
            module.set_log_level("OFF")  # its log names files; keep it out of the server's log
            slide = module.open_slide(path, driver)
            scenes = [slide.get_scene(i) for i in range(slide.num_scenes)]
            report = [describe(scene) for scene in scenes]
        except Exception as err:
            send(conn, {"error": type(err).__name__})
            return
        send(conn, {"ok": report})
        while (request := json.loads(conn.recv_bytes(MESSAGE_MAX)))["op"] == "read":
            answer(conn, scenes, request)


def describe(scene: Any) -> dict[str, Any]:
    """Sizes, sample types, planes, pixel size, codec and levels of a scene."""
    sizes = [scene.get_zoom_level_info(k).size for k in range(scene.num_zoom_levels)]
    return {
        "size": [int(v) for v in scene.size],
        "channels": int(scene.num_channels),
        "dtypes": [
            np.dtype(scene.get_channel_data_type(c)).name for c in range(scene.num_channels)
        ],
        "z": int(scene.num_z_slices),
        "t": int(scene.num_t_frames),
        "resolution": [float(v) for v in scene.resolution],
        "compression": str(scene.compression.name),
        "levels": [[int(s.width), int(s.height)] for s in sizes],
    }


def answer(conn: Connection, scenes: list[Any], request: dict[str, Any]) -> None:
    try:
        x, y, w, h = request["rect"]
        block = scenes[request["scene"]].read_block_from_level(request["level"], (x, y, w, h))
        data = np.ascontiguousarray(block)
    except Exception as err:
        send(conn, {"error": type(err).__name__})
        return
    send(conn, {"ok": {"dtype": data.dtype.name, "shape": list(data.shape)}})
    conn.send_bytes(data.reshape(-1).view(np.uint8))
