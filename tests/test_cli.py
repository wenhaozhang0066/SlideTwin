"""The slidetwin command (M9): check, manifest, replay, fetch, serve and mcp."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
import yaml

from slidetwin import __version__, cli, scan
from slidetwin.core.config import default_config_dir
from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.registry import SlideRegistry
from slidetwin.env import Env
from tests.conftest import CONFIG_DIR
from tests.helpers.dicom_wsi import write_series
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff

VARS = ("SLIDETWIN_MANIFEST", "SLIDETWIN_SLIDE_DIR", "SLIDETWIN_FETCH_SLIDES", "HOST", "PORT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commands set these; monkeypatch puts them back after each test."""
    for var in VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLIDETWIN_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("SLIDETWIN_CACHE_DIR", "")


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """Slides of several kinds: a TIFF, the same TIFF again, a DICOM series, a hidden copy and
    two files that are not slides."""
    d = tmp_path / "slides"
    d.mkdir()
    lines = line_image(1024, 768, (100, 500), (200,))
    write_pyramid_tiff(d / "a.tif", pyramid(lines, 3), 0.5)
    (d / "sub").mkdir()
    shutil.copy(d / "a.tif", d / "sub" / "again.tif")
    write_series(d / "series", pyramid(lines, 3), mpp=(0.5, 0.5))
    (d / ".cache").mkdir()
    shutil.copy(d / "a.tif", d / ".cache" / "hidden.tif")
    (d / "notes.svs").write_text("not a slide")
    tifffile.imwrite(d / "plain.tif", np.zeros((64, 64, 3), np.uint8), tile=(64, 64))  # no mpp
    (d / "readme.txt").write_text("ignored: not a slide name")
    return d


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"slidetwin {__version__}"


def test_check(folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "check", str(folder / "a.tif"), str(folder / "series"), "--hash")
    assert code == 0
    assert "format: generic-tiff (reader tifffile)" in out
    assert "levels: 3, 1024 x 768 px down to 256 x 192 px" in out
    assert "mpp: 0.5000 x 0.5000 um/px" in out
    assert "format: dicom (reader wsidicom)" in out
    assert "files: 3" in out  # three levels; the label and the overview are never part of it
    assert "fileset_sha256: " in out
    code, out, _ = run(capsys, "check", str(folder / "a.tif"))  # no hash unless asked
    assert (code, "sha256" in out) == (0, False)
    code, out, _ = run(capsys, "check", str(folder / "notes.svs"), str(folder / "plain.tif"))
    assert code == 1
    assert out.count("refused: ") == 2
    assert "mpp_override" in out


def test_a_slide_that_opens_but_cannot_be_served_is_refused(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unservable(source: Any, name: str) -> None:
        raise SlideMetadataError(f"Slide '{name}' has no small level.")

    monkeypatch.setattr(scan, "check_servable", unservable)
    result = scan.check(folder / "a.tif")
    assert result.refused == "Slide 'a.tif' has no small level."
    assert result.lines() == [str(folder / "a.tif"), "  refused: Slide 'a.tif' has no small level."]


def test_manifest(folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_file = tmp_path / "manifest.yaml"
    code, _, err = run(capsys, "manifest", str(folder), "-o", str(out_file))
    assert code == 0
    text = out_file.read_text()
    data = yaml.safe_load(text)
    paths = {e["path"]: e for e in data["slides"]}
    assert sorted(paths) == ["a.tif", "series"]  # the copy, hidden and broken files are not
    assert paths["a.tif"]["format"] == "generic-tiff"
    assert paths["series"]["format"] == "dicom"
    for entry in data["slides"]:
        digest = entry.get("sha256") or entry.get("fileset_sha256")
        assert entry["id"] == f"s_{digest[:10]}"
    assert "#   sub/again.tif: the same slide as a.tif" in text
    assert "#   notes.svs: " in text
    assert "#   plain.tif: " in text
    assert "readme" not in text
    assert "hidden" not in text
    assert f"{len(data['slides'])} slides, 3 not served" in err
    registry = SlideRegistry.from_manifest(out_file, folder)  # it serves, hashes checked
    for slide_id in registry.ids():
        assert registry.get(slide_id).source.level_dimensions[0] == (1024, 768)
    registry.close()
    code, out, _ = run(capsys, "manifest", str(folder / "sub"))  # to stdout; nothing refused
    assert code == 0
    assert "Not served" not in out
    assert yaml.safe_load(out)["slides"][0]["path"] == "again.tif"
    assert run(capsys, "manifest", str(folder / "a.tif"))[0] == 2  # not a folder


def test_replay(folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "manifest.yaml"
    cli.main(["manifest", str(folder), "-o", str(manifest)])
    slide_id = yaml.safe_load(manifest.read_text())["slides"][0]["id"]
    with Env(manifest, folder, log_dir=tmp_path / "logs", viewport=(256, 256)) as env:
        env.reset(slide_id)
        env.step({"action": "pan", "dx": 0.5, "dy": 0})
    log = next((tmp_path / "logs" / "sessions").glob("*.jsonl"))
    args = ["--manifest", str(manifest), "--slides", str(folder)]
    code, out, _ = run(capsys, "replay", str(log), *args)
    assert (code, out.splitlines()[0]) == (0, "2 events, 2 images checked, 0 differences")
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    lines[2]["cost_s"] = 99.0
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("".join(json.dumps(line) + "\n" for line in lines))
    code, out, _ = run(capsys, "replay", str(tampered), *args)
    assert code == 1
    assert "seq 2: cost_s" in out
    lines[0]["slide"]["sha256"] = "0" * 64
    tampered.write_text("".join(json.dumps(line) + "\n" for line in lines))
    code, _, err = run(capsys, "replay", str(tampered), *args)
    assert code == 1
    assert "replay refused: " in err


def test_fetch_serve_and_mcp_get_the_slide_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from slidetwin import fetch_slides, serve
    from slidetwin.api import main as api_main
    from slidetwin.mcp_server import server as mcp_server

    seen: list[tuple[str, Any]] = []

    def record(name: str, value: Any = None) -> Any:
        keys = ("SLIDETWIN_MANIFEST", "SLIDETWIN_SLIDE_DIR", "SLIDETWIN_FETCH_SLIDES", "PORT")
        seen.append((name, value, {k: os.environ.get(k) for k in keys}))
        return 0

    monkeypatch.setattr(fetch_slides, "main", lambda argv: record("fetch", argv))
    monkeypatch.setattr(serve, "main", lambda: record("serve"))
    demo = str(default_config_dir() / "slides" / "demo.yaml")
    assert cli.main(["fetch", "--demo", "--only", "a,b"]) == 0
    assert seen[-1][:2] == ("fetch", ["--only", "a,b"])
    assert seen[-1][2]["SLIDETWIN_MANIFEST"] == demo
    assert cli.main(["fetch", "--manifest", "m.yaml", "--slides", "s"]) == 0
    assert seen[-1][1] == []
    assert (seen[-1][2]["SLIDETWIN_MANIFEST"], seen[-1][2]["SLIDETWIN_SLIDE_DIR"]) == (
        "m.yaml",
        "s",
    )
    monkeypatch.delenv("SLIDETWIN_MANIFEST")
    assert cli.main(["serve", "--port", "9002"]) == 0
    assert seen[-1][2]["SLIDETWIN_FETCH_SLIDES"] is None  # only --demo downloads
    assert cli.main(["serve", "--demo", "--port", "9001"]) == 0
    assert seen[-1][2]["SLIDETWIN_FETCH_SLIDES"] == "1"
    assert (seen[-1][2]["SLIDETWIN_MANIFEST"], seen[-1][2]["PORT"]) == (demo, "9001")

    class FakeMCP:
        def run(self, transport: str) -> None:
            record("mcp", transport)

    monkeypatch.setattr(mcp_server, "build_mcp", lambda manager: FakeMCP())
    monkeypatch.setattr(api_main, "build_manager", lambda settings: record("manager", settings))
    monkeypatch.setenv("SLIDETWIN_LOG_DIR", str(tmp_path / "logs"))
    assert cli.main(["mcp", "--manifest", "x.yaml", "--slides", "y"]) == 0
    settings = seen[-2][1]
    assert (settings.manifest, settings.slide_dir) == (Path("x.yaml"), Path("y"))
    assert seen[-1][:2] == ("mcp", "stdio")


def test_candidates_skip_hidden_and_find_dicom_folders(folder: Path) -> None:
    found = [p.relative_to(folder).as_posix() for p in scan.candidates(folder)]
    assert found == ["a.tif", "notes.svs", "plain.tif", "series", "sub/again.tif"]
