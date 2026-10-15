"""Deployment helpers (M7): the slide fetcher and the container entry point."""

from __future__ import annotations

import hashlib
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
    for text in ["present", "sha256 does not match", "no source_url", "download failed", "done in"]:
        assert text in err
    assert not fetch_slides.fetch_all([good, local], tmp_path, http)


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
    assert (runs[1]["host"], runs[1]["port"]) == ("0.0.0.0", 7860)
