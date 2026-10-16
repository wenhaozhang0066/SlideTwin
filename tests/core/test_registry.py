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
from slidetwin.core.fileset import fileset_sha256
from slidetwin.core.registry import SlideEntry, SlideRegistry, file_sha256, parse_manifest
from slidetwin.core.slide_source import ArraySlideSource, ReaderOptions
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


def test_refused_formats_explain_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import slidetwin.readers as readers

    monkeypatch.delitem(readers.READERS, "czi")  # as in a version without that reader
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


def test_reader_choices_are_validated() -> None:
    e = entry(scene=2, plane=0, channels=[0, 3], experimental=True)
    assert e.options() == ReaderOptions(2, 0, (0, 3), True)
    for bad in ({"scene": -1}, {"channels": []}, {"channels": [999]}, {"fileset_sha256": "x"}):
        with pytest.raises(ConfigError):
            parse_manifest({"slides": [{"id": "s", "path": "s.tif", **bad}]})


@pytest.mark.parametrize("kw", [{"scene": 1}, {"format": "aperio-svs"}])
def test_reader_choices_need_backend_auto(slide_dir: Path, kw: dict[str, Any]) -> None:
    for backend in ("tiffslide", "openslide"):
        reg = SlideRegistry([entry(backend=backend, **kw)], slide_dir)
        with pytest.raises(ConfigError, match="need backend auto"):
            reg.get("syn")


def test_slides_without_a_small_enough_level_are_refused(
    slide_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the coarsest level of syn.tif is 256 x 256; a whole-slide view reads it at once
    monkeypatch.setattr(registry_mod, "MAX_READ_PIXELS", 256 * 256)
    assert SlideRegistry([entry()], slide_dir).get("syn").source.level_dimensions[-1] == (256, 256)
    monkeypatch.setattr(registry_mod, "MAX_READ_PIXELS", 256 * 256 - 1)
    with pytest.raises(SlideMetadataError, match=r"no pyramid level small enough .*256 x 256"):
        SlideRegistry([entry()], slide_dir).get("syn")


class _SetSource(ArraySlideSource):
    """A source that reads several files, like an MRXS slide and its data folder."""

    def __init__(self, root: Path, files: tuple[str, ...]) -> None:
        super().__init__([np.zeros((8, 8, 3), np.uint8)], (0.5, 0.5))
        self.fileset = (root, files)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _mrxs(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    (tmp_path / "s").mkdir()
    (tmp_path / "s.mrxs").write_bytes(b"preview")
    (tmp_path / "s" / "Data0000.dat").write_bytes(b"tiles")
    return tmp_path, ("s.mrxs", "s/Data0000.dat")


def test_file_sets_are_hashed_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, files = _mrxs(tmp_path)
    opened: list[_SetSource] = []

    def fake_open(path: Path, e: SlideEntry, override: Any) -> _SetSource:
        opened.append(_SetSource(root, files))
        return opened[-1]

    monkeypatch.setattr(registry_mod, "_open_auto", fake_open)
    whole = fileset_sha256(root, files, file_sha256)
    mrxs_hash = file_sha256(root / "s.mrxs")
    good = [
        entry(id="a", path="s.mrxs"),
        entry(id="b", path="s.mrxs", fileset_sha256=whole, sha256=mrxs_hash),
    ]
    reg = SlideRegistry(good, root, cache_dir=tmp_path / "cache")
    for slide_id in ("a", "b"):  # the entry file's hash (D9) and the hash of the whole set
        assert (reg.get(slide_id).sha256, reg.get(slide_id).fileset_sha256) == (mrxs_hash, whole)
    bad = [
        entry(id="c", path="s.mrxs", fileset_sha256="0" * 64),
        entry(id="d", path="s.mrxs", sha256="0" * 64),
        entry(id="d2", path="s.mrxs", sha256=mrxs_hash),  # covers the entry file only
    ]
    reg = SlideRegistry(bad, root)
    for slide_id, error, text in (
        ("c", SlideIntegrityError, "fileset_sha256"),
        ("d", SlideIntegrityError, "sha256"),
        ("d2", ConfigError, "made of several files"),
    ):
        with pytest.raises(error, match=text):
            reg.get(slide_id)
        assert opened[-1].closed  # a refused slide's handle is closed
    (root / "s" / "Data0000.dat").unlink()
    with pytest.raises(SlideNotFound, match="1 of its 2 files are missing"):
        SlideRegistry([entry(id="e", path="s.mrxs")], root).get("e")


def test_pins_must_fit_the_slide(
    slide_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError, match="single file"):
        SlideRegistry([entry(fileset_sha256="0" * 64)], slide_dir).get("syn")
    (tmp_path / "series").mkdir()  # e.g. a DICOM series, read as a folder
    folder_source = ArraySlideSource([np.zeros((8, 8, 3), np.uint8)], (0.5, 0.5))
    monkeypatch.setattr(registry_mod, "_open_auto", lambda *a: folder_source)
    with pytest.raises(ConfigError, match="is a folder"):
        SlideRegistry([entry(id="f", path="series", sha256="0" * 64)], tmp_path).get("f")


def test_hash_cache_is_read_and_written_once_and_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sets = {}
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        for i in range(40):
            (root / f"f{i:02d}.dat").write_bytes(f"{name}{i}".encode())
        sets[name] = (root, tuple(f"f{i:02d}.dat" for i in range(40)))
    monkeypatch.setattr(
        registry_mod, "_open_auto", lambda path, e, o: _SetSource(*sets[e.path.split("/")[0]])
    )
    reads, writes = [0], [0]
    real_read, real_write = SlideRegistry._read_cache, SlideRegistry._write_cache
    monkeypatch.setattr(
        SlideRegistry,
        "_read_cache",
        staticmethod(lambda f: reads.__setitem__(0, reads[0] + 1) or real_read(f)),
    )
    monkeypatch.setattr(
        SlideRegistry,
        "_write_cache",
        staticmethod(lambda f, c: writes.__setitem__(0, writes[0] + 1) or real_write(f, c)),
    )
    cache = tmp_path / "cache"
    one = SlideRegistry([entry(id="a", path="a/f00.dat")], tmp_path, cache_dir=cache)
    two = SlideRegistry([entry(id="b", path="b/f00.dat")], tmp_path, cache_dir=cache)
    one.get("a")
    assert (reads[0], writes[0]) == (2, 1)  # once before hashing, once to merge: not per file
    two.get("b")  # another registry (another process) writes in between: nothing is lost
    stored = json.loads((cache / "slide_sha256.json").read_text())
    assert len(stored) == 80


def test_unreadable_slides_are_not_found_without_their_path(tmp_path: Path) -> None:
    secret = tmp_path / "patient_SECRET.tif"
    write_pyramid_tiff(secret, pyramid(line_image(64, 64, (1,), (1,)), 1), 0.5)
    secret.chmod(0)
    folder = tmp_path / "series_SECRET"
    folder.mkdir()
    folder.chmod(0)
    reg = SlideRegistry(
        [entry(id="f", path=secret.name), entry(id="d", path=folder.name)], tmp_path
    )
    try:
        for slide_id in ("f", "d"):
            with pytest.raises(SlideNotFound, match="could not be read") as err:
                reg.get(slide_id)
            assert "SECRET" not in err.value.message
    finally:
        secret.chmod(0o644)
        folder.chmod(0o755)
