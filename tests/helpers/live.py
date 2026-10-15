"""A real HTTP server for tests that need one (browser, MCP over HTTP, agents)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI


@contextmanager
def serve(app: FastAPI, timeout_s: float = 20.0) -> Iterator[str]:
    """Run `app` with uvicorn on a free local port in a thread; yields the base URL."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout_s)
