"""Deployment helpers (M7): the slide fetcher, the container entry point and the codespace."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
import yaml

from slidetwin import fetch_slides, serve
from slidetwin.core.registry import parse_manifest

DATA = b"slide-bytes" * 1000
SHA = hashlib.sha256(DATA).hexdigest()


def entries(**extra: Any) -> list[Any]:
    slides = [
        {
            "id": "good",
            "path": "sub/good.tif",
            "source_url": "https://x.test/good.tif",
            "sha256": SHA,
        },
        {
            "id": "bad",
            "path": "bad.tif",
            "source_url": "https://x.test/bad.tif",
            "sha256": "0" * 64,
        },
        {"id": "local", "path": "local.tif"},
        {"id": "down", "path": "down.tif", "source_url": "https://x.test/missing.tif"},
        {"id": "loose", "path": "loose.tif", "source_url": "https://x.test/good.tif", **extra},
    ]
    return parse_manifest({"slides": slides})


def client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("missing.tif"):
            return httpx.Response(404)
        return httpx.Response(200, content=DATA, headers={"content-length": str(len(DATA))})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_checks_hashes_and_keeps_good_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good, bad, local, down, loose = entries()
    http = client()
    assert fetch_slides.fetch_one(good, tmp_path, http)
    assert (tmp_path / "sub" / "good.tif").read_bytes() == DATA
    assert fetch_slides.fetch_one(good, tmp_path, http)  # present: kept
    assert not fetch_slides.fetch_one(bad, tmp_path, http)  # hash mismatch: discarded
    assert not (tmp_path / "bad.tif").exists()
    assert not list(tmp_path.glob(".*.part"))
    assert not fetch_slides.fetch_one(local, tmp_path, http)
    assert not fetch_slides.fetch_one(down, tmp_path, http)
    assert fetch_slides.fetch_one(loose, tmp_path, http)  # no pin: any content is kept
    err = capsys.readouterr().err
    for text in [
        "present",
        "sha256 does not match",
        "no source_url",
        "failed (HTTPStatusError",
        "done in",
    ]:
        assert text in err
    assert not fetch_slides.fetch_all([good, local], tmp_path, http)


def test_fetch_never_raises_on_a_read_only_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good, *_ = entries()
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert not fetch_slides.fetch_one(good, locked, client())
    finally:
        locked.chmod(0o700)
    assert "failed (PermissionError" in capsys.readouterr().err
    assert not list(locked.rglob("*"))


def test_fetch_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({"slides": [{"id": "local", "path": "local.tif"}]}))
    (tmp_path / "local.tif").write_bytes(b"x")
    args = ["--manifest", str(manifest), "--slide-dir", str(tmp_path)]
    assert fetch_slides.main(args) == 0
    assert fetch_slides.main([*args, "--only", "nope"]) == 1
    calls: list[Any] = []
    monkeypatch.setattr(fetch_slides, "fetch_all", lambda chosen, d: calls.append(chosen) or True)
    assert fetch_slides.main([*args, "--only", "local"]) == 0
    assert [e.id for e in calls[0]] == ["local"]


def test_entry_point_fetches_then_serves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: runs.append({"app": app, **kw}))
    fetched: list[Any] = []
    monkeypatch.setattr(serve.fetch_slides, "main", lambda argv: fetched.append(argv) or 1)
    serve.main({"SLIDETWIN_FETCH_SLIDES": "1", "PORT": "9000", "FORWARDED_ALLOW_IPS": "*"})
    assert fetched == [[]]
    assert "could not be fetched" in capsys.readouterr().err
    assert runs[0]["app"] == "slidetwin.api.main:app"
    assert (runs[0]["port"], runs[0]["workers"], runs[0]["proxy_headers"]) == (9000, 1, True)
    assert runs[0]["forwarded_allow_ips"] == "*"
    serve.main({})
    assert len(fetched) == 1  # not asked to fetch

    def broken(argv: list[str]) -> int:
        raise ValueError("manifest unreadable")

    monkeypatch.setattr(serve.fetch_slides, "main", broken)
    serve.main({"SLIDETWIN_FETCH_SLIDES": "1"})
    assert len(runs) == 3  # the server still starts
    assert "manifest unreadable" in capsys.readouterr().err
    assert (runs[1]["host"], runs[1]["port"]) == ("0.0.0.0", 7860)


DEVCONTAINER = Path(__file__).resolve().parents[1] / ".devcontainer"
needs_util_linux = pytest.mark.skipif(
    not (shutil.which("setsid") and shutil.which("flock")), reason="needs setsid and flock"
)


def test_devcontainer_runs_the_scripts_and_forwards_the_port() -> None:
    config = json.loads((DEVCONTAINER / "devcontainer.json").read_text(encoding="utf-8"))
    steps = ("updateContentCommand", "postCreateCommand", "postStartCommand", "postAttachCommand")
    for step in steps:
        command = config[step].split()
        assert command[0] == "bash"
        assert (DEVCONTAINER.parent / command[1]).is_file()
    assert config["forwardPorts"] == [7860]  # serve.py's default port
    assert config["portsAttributes"]["7860"]["label"] == "SlideTwin"


def fake_repo(tmp_path: Path, *scripts: str) -> Path:
    """A repository copy with the given dev container scripts and a .venv that runs this Python."""
    root = tmp_path / "repo"
    (root / ".devcontainer").mkdir(parents=True)
    for name in scripts:
        shutil.copy(DEVCONTAINER / name, root / ".devcontainer")
    (root / ".venv" / "bin").mkdir(parents=True)
    python = root / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')  # keeps this environment
    python.chmod(0o755)
    return root


def codespace_env(**extra: str) -> dict[str, str]:
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("SLIDETWIN_", "CODESPACE"))}
    return {**keep, **extra}


@needs_util_linux
def test_codespace_fetch_script_downloads_once_per_manifest(tmp_path: Path) -> None:
    root = fake_repo(tmp_path, "fetch.sh")
    manifest = root / "configs" / "slides" / "manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(yaml.safe_dump({"slides": [{"id": "local", "path": "local.tif"}]}))
    slides = root / "data" / "slides"
    slides.mkdir(parents=True)
    (slides / "local.tif").write_bytes(b"x")

    def fetch() -> subprocess.CompletedProcess[str]:
        script = root / ".devcontainer" / "fetch.sh"
        return subprocess.run(
            ["bash", script], env=codespace_env(), capture_output=True, text=True, check=False
        )

    assert fetch().returncode == 0
    stamp = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert (slides / ".fetched").read_text().strip() == stamp
    (slides / "local.tif").unlink()
    again = fetch()  # same manifest: nothing is fetched or checked again
    assert (again.returncode, again.stderr) == (0, "")
    with (root / "logs" / ".fetch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert "already being downloaded" in fetch().stdout
    manifest.write_text(yaml.safe_dump({"slides": [{"id": "gone", "path": "gone.tif"}]}))
    failed = fetch()  # a failed download is reported, never fatal, and retried next time
    assert failed.returncode == 0
    assert "could not be downloaded" in failed.stderr
    assert (slides / ".fetched").read_text().strip() == stamp


@needs_util_linux
def test_codespace_start_script_detaches_the_server_once(tmp_path: Path) -> None:
    root = fake_repo(tmp_path, "start.sh")
    (root / ".devcontainer" / "fetch.sh").write_text("echo ran >logs/fetch-ran\n")  # no network
    (root / "configs").symlink_to(DEVCONTAINER.parent / "configs")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = codespace_env(
        HOST="127.0.0.1",
        PORT=str(port),
        CODESPACE_NAME="test-cs",
        GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="app.github.dev",
    )
    forwarded = f"test-cs-{port}.app.github.dev"

    def start(**extra: str) -> subprocess.CompletedProcess[str]:
        script = root / ".devcontainer" / "start.sh"
        return subprocess.run(
            ["bash", script],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def status(host: str) -> int:
        with httpx.Client() as http:
            return http.get(
                f"http://127.0.0.1:{port}/api/health", headers={"host": host}
            ).status_code

    def post_from(origin: str) -> int:
        # The relay rewrites Host, and the Origin of the page's own requests, to localhost.
        headers = {"host": f"localhost:{port}", "origin": origin}
        with httpx.Client() as http:
            url = f"http://127.0.0.1:{port}/api/sessions"
            return http.post(url, json={}, headers=headers).status_code

    first = start(SLIDETWIN_FETCH_SLIDES="0")
    pid_file = root / "logs" / "server.pid"
    pid = int(pid_file.read_text()) if pid_file.exists() else None
    try:
        assert first.returncode == 0, first.stderr
        assert f"SlideTwin is running: https://{forwarded}" in first.stdout
        assert (status(forwarded), status(f"localhost:{port}"), status("evil.test")) == (
            200,
            200,
            421,
        )
        assert post_from(f"http://localhost:{port}") == 422  # allowed; the empty body is refused
        assert post_from(f"https://{forwarded}") == 422
        assert post_from("https://evil.test") == 403
        assert not (root / "logs" / "fetch-ran").exists()
        again = start()  # attach after start: the running server is kept
        assert again.returncode == 0, again.stderr
        assert int(pid_file.read_text()) == pid
        assert "still downloading" in again.stdout  # no slides fetched yet: resume the download
        for _ in range(100):
            if (root / "logs" / "fetch-ran").exists():
                break
            time.sleep(0.05)
        assert (root / "logs" / "fetch-ran").exists()
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGTERM)
