"""Slide libraries in worker processes (M8): slideio for VSI and ZVI, pyisyntax for iSyntax.

Both libraries can take the whole process down. slideio crashed it on several formats in the survey
(SIGSEGV, SIGFPE, glibc aborts), and its Slide.get_scene_by_name() segfaults; libisyntax crashes on
truncated copies and loops forever on others (review r04). So neither runs in the server: each open
slide has a worker, a fresh Python process that opens it once and answers read requests over a
socket. A crash, a timeout or a damaged answer fails the request in flight with an error; the next
request starts a new worker, which must report the same slide as before. Reads are deterministic, so
a new worker serves the same pixels. A worker has one thread, so libisyntax's rule that every call
comes from the thread that made the first one holds by itself.

The worker is started with a small bootstrap (python -I -c), not with multiprocessing's spawn: spawn
runs the server's main module again in the child, which breaks scripts without a main guard. The
file to open travels over the socket, so it never shows in the process list. The socket carries
JSON messages and raw pixel bytes, never pickles: a worker that a crafted file has taken over cannot
run code in the server, and every message is checked and bounded.

What a worker runs is an adapter, a module with two functions that are called in the worker only:
`open_slide(path, options)` opens the file and returns the open slide and a report on it (JSON
numbers and type names, never names or text from the file), and `read(slide, request)` returns the
pixels a read request asks for. The server checks the report with a parser of the adapter's.
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
from collections.abc import Callable, Sequence
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.errors import SlideMetadataError, SlideReadError

TIMEOUT_S = 120.0  # for an open or a read; a worker that takes longer is stopped
STOP_S = 2.0  # how long a worker may take to exit once its socket is closed
MESSAGE_MAX = 1 << 20  # bytes of one JSON message
DTYPES = frozenset({"uint8", "int16", "uint16"})  # sample types a worker may send
# The exit status of a worker whose slide file ends before data that the file's own header lists
# (an incomplete copy): an adapter that finds this ends its worker at once with it, and the request
# fails with that reason.
TRUNCATED = 86
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")  # an exception type, a codec or a dtype
# The worker's first lines: take the server's import path, then serve. Isolated mode (-I) keeps
# the current folder and the environment out of the imports made before that.
_BOOT = (
    "import json, sys\n"
    "from multiprocessing.connection import Connection\n"
    "conn = Connection(int(sys.argv[1]))\n"
    "start = json.loads(conn.recv_bytes())\n"
    "sys.path[:] = start['path']\n"
    "from slidetwin.readers.worker import serve\n"
    "serve(conn, start['adapter'], start['file'], start['options'])\n"
)

STOPPED = "the worker stopped"
DAMAGED = "the worker sent a damaged answer"
SHORT = "the slide file is shorter than its header says"


def library_version(module: str, distribution: str | None = None) -> str:
    """The version of a library that only workers import (`distribution` when its pip name differs
    from its module's). A missing library raises ModuleNotFoundError, which open_source reports with
    the extra to install."""
    if importlib.util.find_spec(module) is None:
        raise ModuleNotFoundError(f"No module named '{module}'", name=module)
    return importlib.metadata.version(distribution or module)


class WorkerError(Exception):
    """A request failed. `reported` is true when the library raised (the worker is still fine), and
    `stopped` when the worker ended before it answered (it crashed or was killed); otherwise it
    took too long or broke the protocol."""

    def __init__(self, reason: str, *, reported: bool = False, stopped: bool = False) -> None:
        super().__init__(reason)
        self.reported = reported
        self.stopped = stopped


class Worker[Info]:
    """One slide, opened by a library in its own process.

    `adapter` names the module whose functions run in the worker, and `options` (JSON) go to its
    open_slide. `parse` checks the worker's report on the slide it opened and gives `info`; a worker
    started again later must report the same. `library` names the backend in errors.
    """

    def __init__(
        self,
        path: Path,
        name: str,
        *,
        library: str,
        adapter: str,
        options: dict[str, Any],
        parse: Callable[[Any], Info],
    ) -> None:
        self._start_message = {
            "path": [p for p in sys.path if isinstance(p, str)],
            "file": str(path),
            "adapter": adapter,
            "options": options,
        }
        self._name = name
        self._parse = parse
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._conn: Connection | None = None
        self._closed = False
        try:
            self.info = self._start()
        except WorkerError as err:
            raise SlideMetadataError(
                f"Slide '{name}' could not be opened with the {library} backend ({err}).",
                hint="The slide's files may be damaged or incompletely copied.",
            ) from None

    def read_block(self, request: dict[str, Any], level: int, hw: tuple[int, int]) -> NDArray[Any]:
        """The block of size `hw` that a read request asks for, (h, w) or (h, w, channels), as the
        library returns it; `level` is named in errors. After a crash, a timeout or a broken
        message the worker is stopped, and the next read starts a new one; so does a read that
        finds its worker gone."""
        with self._lock:
            try:
                if self._closed:
                    raise WorkerError("the slide is closed")
                if self._process is not None and self._process.poll() is not None:
                    self._stop()  # it ended between reads
                if self._conn is None and self._start() != self.info:
                    self._stop()
                    raise WorkerError("the slide changed on disk since it was opened")
                assert self._conn is not None
                return request_block(self._conn, request, hw)
            except WorkerError as err:
                reason = err if err.reported else self._end(err)
                raise SlideReadError(
                    f"Slide '{self._name}' could not be decoded at level {level} ({reason}).",
                    hint="The slide's files may be damaged; the next read starts a new worker.",
                ) from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                with contextlib.suppress(OSError):
                    send(self._conn, {"op": "close"})
            self._stop()

    def _start(self) -> Info:
        """A new worker and its report. A worker that stops before it answers is started once
        more: such a stop can pass (a signal from outside, a shortage of memory), and the next
        attempt then works (review r16). A second stop is reported."""
        try:
            return self._launch()
        except WorkerError as err:
            if not err.stopped:
                raise
        return self._launch()

    def _launch(self) -> Info:
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
            return self._parse(receive(self._conn, TIMEOUT_S))
        except OSError:
            raise self._end(WorkerError(STOPPED, stopped=True)) from None
        except WorkerError as err:
            raise self._end(err) from None

    def _end(self, err: WorkerError) -> WorkerError:
        """Stop the worker after `err`. One that ended because its file is truncated says so with
        its exit status, which then gives the reason."""
        return WorkerError(SHORT) if self._stop() == TRUNCATED else err

    def _stop(self) -> int | None:
        """End the worker and return its exit status (None if there was none): it exits once its
        socket is closed; one that hangs is killed."""
        conn, process = self._conn, self._process
        self._conn = self._process = None
        if conn is not None:
            conn.close()
        if process is None:
            return None
        try:
            return process.wait(STOP_S)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


# --- the protocol ------------------------------------------------------------------------------


def send(conn: Connection, message: Any) -> None:
    conn.send_bytes(json.dumps(message).encode("ascii"))


def request_block(conn: Connection, request: dict[str, Any], hw: tuple[int, int]) -> NDArray[Any]:
    """Send a read request; the block, as announced by its header and of exactly that size."""
    try:
        send(conn, request)
    except OSError:
        raise WorkerError(STOPPED, stopped=True) from None
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
        raise WorkerError(f"the worker took longer than {timeout:g} s")
    try:
        return conn.recv_bytes(limit)
    except (EOFError, ConnectionError):  # it ended, also while a request was on its way
        raise WorkerError(STOPPED, stopped=True) from None
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


# Checks for the parsers of reports; they raise ValueError, which a parser turns into DAMAGED.


def ints(values: Sequence[Any], n: int) -> tuple[int, ...]:
    if len(values) != n or not all(type(v) is int and v >= 0 for v in values):
        raise ValueError("expected non-negative integers")
    return tuple(values)


def floats(values: Sequence[Any], n: int) -> tuple[float, ...]:
    if len(values) != n or not all(type(v) in (int, float) for v in values):
        raise ValueError("expected numbers")
    return tuple(float(v) for v in values)


def names(values: Sequence[Any]) -> tuple[str, ...]:
    if not all(isinstance(v, str) and _NAME.fullmatch(v) for v in values):
        raise ValueError("expected type names")
    return tuple(values)


# --- the worker process --------------------------------------------------------------------------


def serve(conn: Connection, adapter: str, path: str, options: dict[str, Any]) -> None:
    """The worker: open the slide with the adapter and report on it, then answer reads until the
    server asks it to stop or closes the socket."""
    with contextlib.suppress(EOFError, OSError):  # the server closed the socket
        try:
            module = importlib.import_module(adapter)
            slide, report = module.open_slide(path, options)
        except Exception as err:
            send(conn, {"error": type(err).__name__})
            return
        send(conn, {"ok": report})
        while (request := json.loads(conn.recv_bytes(MESSAGE_MAX)))["op"] == "read":
            answer(conn, module.read, slide, request)


def answer(
    conn: Connection,
    read: Callable[[Any, dict[str, Any]], Any],
    slide: Any,
    request: dict[str, Any],
) -> None:
    """One read: the block's sample type and shape, then its bytes; or the library's error type."""
    try:
        data = np.ascontiguousarray(read(slide, request))
    except Exception as err:
        send(conn, {"error": type(err).__name__})
        return
    send(conn, {"ok": {"dtype": data.dtype.name, "shape": list(data.shape)}})
    conn.send_bytes(data.reshape(-1).view(np.uint8))
