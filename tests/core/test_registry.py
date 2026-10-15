from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
import yaml

from slidetwin.core import registry as registry_mod
from slidetwin.core.errors import ConfigError, SlideIntegrityError, SlideNotFound
from slidetwin.core.registry import SlideEntry, SlideRegistry, file_sha256, parse_manifest
from tests.conftest import CONFIG_DIR
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff


@pytest.fixture(scope="module")
def slide_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("slides")
    write_pyramid_tiff(d / "syn.tif", pyramid(line_image(512, 512, (100,), (100,)), 2), 0.5)
    tifffile.imwrite(d / "nompp.tif", np.full((256, 256, 3), 200, np.uint8), tile=(256, 256))
    return d


def entry(**kw: Any) -> SlideEntry:
    kw.setdefault("id", "syn")
    kw.setdefault("path", "syn.tif")
    return SlideEntry.model_validate(kw)


def test_repo_manifest_parses_and_drops_ground_truth() -> None:
    data = yaml.safe_load((CONFIG_DIR / "slides" / "manifest.yaml").read_text())
    assert any("eval_only" in s for s in data["slides"])
    entries = parse_manifest(data)
    ids = [e.id for e in entries]
    assert ids[0] == "cmu1_small"
    assert "c16_tumor_082" in ids
    for e in entries:
        dumped = json.dumps(e.model_dump())
        assert "eval_only" not in dumped
        assert "macro" not in dumped
        assert "micro" not in dumped
        assert "annotation" not in dumped


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"slides": "x"},
        {"slides": ["x"]},
        {"slides": [{"id": "a", "path": "/abs/a.svs"}]},
        {"slides": [{"id": "a", "path": "../a.svs"}]},
        {"slides": [{"id": "a", "path": ""}]},
        {"slides": [{"id": "bad id", "path": "a.svs"}]},
        {"slides": [{"id": "a", "path": "a.svs", "sha256": "xyz"}]},
        {"slides": [{"id": "a", "path": "a.svs", "labels": "tumor"}]},
        {"slides": [{"id": "a", "path": "a.svs"}, {"id": "a", "path": "b.svs"}]},
    ],
)
def test_invalid_manifests(data: Any) -> None:
    with pytest.raises(ConfigError):
        parse_manifest(data)


def test_manifest_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        SlideRegistry.from_manifest(tmp_path / "missing.yaml", tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("slides: [unclosed\n")
    with pytest.raises(ConfigError):
        SlideRegistry.from_manifest(bad, tmp_path)


def test_get_opens_hashes_and_caches(slide_dir: Path, tmp_path: Path) -> None:
    digest = file_sha256(slide_dir / "syn.tif")
    reg = SlideRegistry([entry(sha256=digest)], slide_dir, cache_dir=tmp_path / "cache")
    first = reg.get("syn")
    assert first.sha256 == digest
    assert first.source.mpp == pytest.approx((0.5, 0.5))
    assert reg.get("syn") is first
    assert reg.entry("syn") is not None
    assert reg.entry("nope") is None
    assert reg.ids() == ["syn"]
    cache = json.loads((tmp_path / "cache" / "slide_sha256.json").read_text())
    assert list(cache.values()) == [digest]
    reg.close()


def test_hash_cache_is_reused(
    slide_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn")
    calls: list[Path] = []
    monkeypatch.setattr(registry_mod, "file_sha256", lambda p: calls.append(p) or "f" * 64)
    SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn")
    assert calls == []
    (cache_dir / "slide_sha256.json").write_text("{not json")
    assert SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn").sha256 == "f" * 64
    assert len(calls) == 1


def test_no_cache_dir_still_hashes(slide_dir: Path) -> None:
    reg = SlideRegistry([entry()], slide_dir)
    assert reg.get("syn").sha256 == file_sha256(slide_dir / "syn.tif")


def test_pinned_hash_mismatch_is_refused(slide_dir: Path) -> None:
    reg = SlideRegistry([entry(sha256="0" * 64)], slide_dir)
    with pytest.raises(SlideIntegrityError):
        reg.get("syn")


def test_unknown_and_missing_slides(slide_dir: Path) -> None:
    reg = SlideRegistry([entry(id="gone", path="gone.svs")], slide_dir)
    with pytest.raises(SlideNotFound, match="Unknown slide 'x'"):
        reg.get("x")
    with pytest.raises(SlideNotFound):
        reg.get("gone")


def test_mpp_override_from_manifest(slide_dir: Path) -> None:
    override = {"x": 0.25, "y": 0.25, "source": "scanner spec sheet"}
    reg = SlideRegistry([entry(id="n", path="nompp.tif", mpp_override=override)], slide_dir)
    assert reg.get("n").source.mpp == (0.25, 0.25)


def test_add_source(synthetic: Any) -> None:
    reg = SlideRegistry([], Path("."))
    reg.add_source("mem", synthetic, "a" * 64)
    assert reg.ids() == ["mem"]
    assert reg.get("mem").source is synthetic
