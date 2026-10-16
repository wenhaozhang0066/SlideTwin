"""The worker process protocol (M8), shared by slideio and pyisyntax: messages, block headers and
reported errors, with the test as the other end of the socket. Real worker processes are tested
through their adapters, in test_slideio_worker.py and test_isyntax.py."""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Iterator
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from typing import Any

import pytest

import slidetwin.readers.worker as shared
from slidetwin.readers.worker import (
    DAMAGED,
    WorkerError,
    block_header,
    library_version,
    receive,
    request_block,
    send,
)


def test_a_missing_library() -> None:
    assert library_version("slideio") == "2.10.0"
    assert library_version("isyntax", "pyisyntax") == "0.1.7"  # module and pip names differ
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
    "data",  # nested deep enough for RecursionError, and small enough for a pipe nothing reads
    [b"not json", b"[" * 50_000, b'["ok"]', b'{"ok": 1, "error": "X"}', b'{"done": 1}'],
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
    os.write(theirs.fileno(), struct.pack("!i", shared.MESSAGE_MAX + 1))
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
