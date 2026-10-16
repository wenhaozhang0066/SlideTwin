from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
import yaml

from slidetwin.core import registry as registry_mod
from slidetwin.core.errors import (
    ConfigError,
    SlideIntegrityError,
    SlideMetadataError,
    SlideNotFound,
)
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
    assert "c16_fa751c" in ids
    for e in entries:
        dumped = json.dumps(e.public())
        assert "eval_only" not in dumped
        for word in ("tumor", "normal", "macro", "micro", "negative", "annotation"):
            assert word not in dumped.lower(), (e.id, word)


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
    reg = SlideRegistry([entry()], slide_dir, cache_dir=tmp_path / "cache")
    first = reg.get("syn")
    assert first.sha256 == digest
    assert first.source.mpp == pytest.approx((0.5, 0.5))
    assert reg.get("syn") is first
    assert reg.entry("syn") is not None
    assert reg.entry("nope") is None
    assert reg.ids() == ["syn"]
    assert reg.catalogue() == [{"id": "syn", "license": None, "attribution": None}]
    cache = json.loads((tmp_path / "cache" / "slide_sha256.json").read_text())
    assert list(cache.values()) == [digest]
    reg.close()


def test_pinned_hash_is_checked_on_the_file_not_the_cache(
    slide_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = file_sha256(slide_dir / "syn.tif")
    cache_dir = tmp_path / "cache"
    SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn")  # fills the cache
    forged = {k: "0" * 64 for k in json.loads((cache_dir / "slide_sha256.json").read_text())}
    (cache_dir / "slide_sha256.json").write_text(json.dumps(forged))
    with pytest.raises(SlideIntegrityError):
        SlideRegistry([entry(sha256="0" * 64)], slide_dir, cache_dir=cache_dir).get("syn")
    assert SlideRegistry([entry(sha256=digest)], slide_dir, cache_dir=cache_dir).get("syn")


def test_hash_cache_is_reused(
    slide_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn")
    calls: list[Path] = []
    monkeypatch.setattr(registry_mod, "file_sha256", lambda p: calls.append(p) or "f" * 64)
    SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn")
    assert calls == []
    for junk in ("{not json", "[]", "null", '{"k": 12345}'):
        (cache_dir / "slide_sha256.json").write_text(junk)
        assert (
            SlideRegistry([entry()], slide_dir, cache_dir=cache_dir).get("syn").sha256 == "f" * 64
        )
    assert len(calls) == 4


def test_unwritable_cache_is_not_fatal(slide_dir: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "cache"
    blocker.write_text("a file where the cache dir should be")
    reg = SlideRegistry([entry()], slide_dir, cache_dir=blocker)
    assert reg.get("syn").sha256 == file_sha256(slide_dir / "syn.tif")


def test_ids_must_not_reveal_ground_truth() -> None:
    leaky = {"slides": [{"id": "c16_tumor_001", "path": "a.tif", "eval_only": {"label": "tumor"}}]}
    with pytest.raises(ConfigError, match="exposes ground truth"):
        parse_manifest(leaky)
    leaky_attr = {
        "slides": [
            {
                "id": "s1",
                "path": "a.tif",
                "attribution": "a macro case",
                "eval_only": {"category": "macro"},
            }
        ]
    }
    with pytest.raises(ConfigError, match="attribution"):
        parse_manifest(leaky_attr)
    ok = {"slides": [{"id": "s1", "path": "a.tif", "eval_only": {"label": "tumor", "x": "ab"}}]}
    assert parse_manifest(ok)[0].id == "s1"


def test_broken_slide_file_names_only_the_slide_id(tmp_path: Path) -> None:
    (tmp_path / "tumor_009.tif").write_bytes(b"not a tiff at all")
    reg = SlideRegistry([entry(id="s9", path="tumor_009.tif")], tmp_path)
    with pytest.raises(SlideMetadataError) as err:
        reg.get("s9")
    assert "s9" in err.value.message
    assert "tumor" not in err.value.message


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


def test_named_backends_still_work_and_auto_is_the_default(slide_dir: Path) -> None:
    assert entry().backend == "auto"
    reg = SlideRegistry([entry(), entry(id="ts", backend="tiffslide")], slide_dir)
    assert reg.get("syn").source.backend.startswith("tifffile")
    assert reg.get("ts").source.backend.startswith("tiffslide")
    a, b = reg.get("syn").source, reg.get("ts").source
    np.testing.assert_array_equal(a.read_level(1, 0, 0, 256, 256), b.read_level(1, 0, 0, 256, 256))
    reg.close()


def test_expected_format_is_checked(slide_dir: Path) -> None:
    reg = SlideRegistry(
        [entry(format="generic-tiff"), entry(id="svs", format="aperio-svs")], slide_dir
    )
    assert reg.get("syn").source.provenance["format"] == "generic-tiff"
    with pytest.raises(SlideMetadataError, match="is generic-tiff, not aperio-svs"):
        reg.get("svs")
    reg.close()


def test_refused_formats_explain_what_to_do(tmp_path: Path) -> None:
    (tmp_path / "scan.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    (tmp_path / "scan.czi").write_bytes(b"ZISRAWFILE" + b"\x00" * 64)
    reg = SlideRegistry(
        [entry(id="jpg", path="scan.jpg"), entry(id="czi", path="scan.czi")], tmp_path
    )
    with pytest.raises(SlideMetadataError, match="cannot read") as err:
        reg.get("jpg")
    assert "pyramidal TIFF" in (err.value.hint or "")
    with pytest.raises(SlideMetadataError, match="does not have yet"):
        reg.get("czi")
