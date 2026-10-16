"""Reader plugins (M9): asked only about files the router has no reader for."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from slidetwin.core import formats
from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import BUILT_IN_FORMATS, FormatMatch
from slidetwin.core.registry import SlideEntry, SlideRegistry
from slidetwin.core.slide_source import ArraySlideSource, source_provenance
from slidetwin.readers import open_source, plugins, route
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff


@dataclass
class EntryPoint:
    """Stands in for an importlib.metadata entry point."""

    name: str
    target: Any
    version: str | None = "1.2.3"
    loads: list[int] = field(default_factory=list)

    def load(self) -> Any:
        self.loads.append(1)
        return self.target

    @property
    def dist(self) -> Any:
        if self.version is None:
            return None
        return SimpleNamespace(version=self.version, metadata={"Name": f"slidetwin-{self.name}"})


def plugin(claim: Any, formats: tuple[str, ...] = ("acme-slide",), opener: Any = None) -> Any:
    def open_reader(path: Path, match: FormatMatch, **kw: Any) -> Any:
        if opener is not None:
            return opener(path, match, **kw)
        image = line_image(512, 384, (100,), (50,))
        return ArraySlideSource(pyramid(image, 2), (0.5, 0.5), name=kw["name"])

    sniff = claim if callable(claim) else (lambda path: claim)
    return SimpleNamespace(FORMATS=formats, sniff=sniff, open_reader=open_reader)


def install(monkeypatch: pytest.MonkeyPatch, *eps: EntryPoint) -> None:
    monkeypatch.setattr(plugins, "entry_points", lambda: sorted(eps, key=lambda e: e.name))


def write(tmp_path: Path, head: bytes, name: str = "x.slide") -> Path:
    path = tmp_path / name
    path.write_bytes(head + bytes(64))
    return path


def test_a_plugin_reads_a_file_the_router_does_not_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, EntryPoint("acme", plugin("acme-slide")))
    path = write(tmp_path, b"ACME")
    assert route(path, "s1") == FormatMatch("acme-slide", "plugin:acme")
    entry = SlideEntry.model_validate({"id": "s1", "path": "x.slide", "format": "acme-slide"})
    registry = SlideRegistry([entry], tmp_path)
    source = registry.get("s1").source
    assert source.level_dimensions == ((512, 384), (256, 192))  # the plugin's own source
    prov = source_provenance(source)
    assert (prov["reader"], prov["versions"]["plugin:acme"]) == ("plugin:acme", "1.2.3")
    assert (prov["format"], prov["plugin_distribution"]) == ("acme-slide", "slidetwin-acme")
    assert source.backend == "plugin:acme 1.2.3"  # SlideTwin's names, not the plugin's
    registry.close()


def test_a_format_the_router_names_must_keep_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdpc = write(tmp_path, b"SQ1.")  # SQRAY SDPC: known, with no open reader
    assert route(sdpc).format == "sqray-sdpc"
    install(monkeypatch, EntryPoint("sdpc", plugin("sqray-sdpc", ("sqray-sdpc",))))
    assert route(sdpc) == FormatMatch("sqray-sdpc", "plugin:sdpc")
    install(monkeypatch, EntryPoint("other", plugin("acme-slide")))
    assert route(sdpc).reader is None  # another name for a format the router knows


@pytest.mark.parametrize(
    "claim",
    [
        "aperio-svs",  # a built-in name for a file the router does not know
        "not-listed",  # not among the plugin's FORMATS
        "Bad Name",
        None,
        7,
    ],
)
def test_claims_that_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim: Any
) -> None:
    formats_ = ("aperio-svs", "acme-slide", "Bad Name")
    install(monkeypatch, EntryPoint("acme", plugin(claim, formats_)))
    path = write(tmp_path, b"FAKE")
    assert route(path) == formats.sniff(path)
    with pytest.raises(SlideMetadataError, match="cannot read \\(unknown\\)"):
        open_source(path, route(path), name="s1")


def test_plugins_are_asked_in_name_order_and_a_broken_one_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path) -> str:
        raise RuntimeError("a bug in the plugin")

    install(
        monkeypatch,
        EntryPoint("c-later", plugin("acme-slide")),
        EntryPoint("a-broken", plugin(broken)),
        EntryPoint("b-first", plugin("acme-slide")),
    )
    assert route(write(tmp_path, b"ACME")).reader == "plugin:b-first"


def test_built_in_formats_never_reach_a_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Path] = []
    install(monkeypatch, EntryPoint("greedy", plugin(lambda p: asked.append(p) or "acme-slide")))
    tif = tmp_path / "a.tif"
    write_pyramid_tiff(tif, pyramid(line_image(256, 256, (10,), (10,)), 2), 0.5)
    assert route(tif).reader == "tifffile"
    assert asked == []


def test_open_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path, b"ACME")
    match = FormatMatch("acme-slide", "plugin:acme")

    def damaged(*a: Any, **kw: Any) -> Any:
        raise ValueError("damaged /secret/path")

    def refuses(*a: Any, name: str, **kw: Any) -> Any:
        raise SlideMetadataError(f"Slide '{name}' is a z-stack.")

    def exits(*a: Any, **kw: Any) -> Any:
        raise SystemExit(3)

    for opener, kind in (
        (damaged, "ValueError"),
        (refuses, "SlideMetadataError"),
        (exits, "SystemExit"),
    ):
        install(monkeypatch, EntryPoint("acme", plugin("acme-slide", opener=opener)))
        with pytest.raises(
            SlideMetadataError, match=f"by the acme reader plugin \\({kind}\\)"
        ) as err:
            open_source(path, match, name="s1")
        assert "secret" not in err.value.message
        assert "z-stack" not in err.value.message  # a plugin's own text may name files
    install(monkeypatch)
    with pytest.raises(SlideMetadataError, match="plugin 'acme', which is not installed"):
        open_source(path, match, name="s1")
    install(monkeypatch, EntryPoint("acme", plugin("acme-slide"), version=None))
    source = open_source(path, match, name="s1")
    assert source_provenance(source)["versions"]["plugin:acme"] == "unknown"
    assert isinstance(source.read_level(1, 0, 0, 8, 8), np.ndarray)


def test_installed_plugins_are_listed() -> None:
    assert isinstance(plugins.entry_points(), list)  # none in this environment


def test_every_router_format_is_a_built_in_name() -> None:
    """A format the router can give must be reserved, or a plugin could take its name."""
    text = Path(formats.__file__).read_text()
    named = set(re.findall(r'FormatMatch\("([a-z0-9-]+)"', text))
    named |= set(formats.NO_READER) | set(formats._VENDOR_MAGIC.values())
    assert named <= BUILT_IN_FORMATS


def test_two_plugins_with_one_name_are_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(
        monkeypatch,
        EntryPoint("acme", plugin(lambda p: None)),  # declines
        EntryPoint("acme", plugin("acme-slide"), version="2.0"),  # would claim
    )
    path = write(tmp_path, b"ACME")
    assert route(path).reader is None
    with pytest.raises(SlideMetadataError, match="or installed twice"):
        open_source(path, FormatMatch("acme-slide", "plugin:acme"), name="s1")


def test_a_plugin_that_exits_while_sniffing_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exits(path: Path) -> str:
        raise SystemExit(3)

    install(monkeypatch, EntryPoint("a", plugin(exits)), EntryPoint("b", plugin("acme-slide")))
    assert route(write(tmp_path, b"ACME")).reader == "plugin:b"


def test_names_plugins_may_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, EntryPoint("kfb", plugin("kfbio-kfb", ("kfbio-kfb",))))
    assert route(write(tmp_path, b"KFB?")).format == "kfbio-kfb"  # known by name, no reader
    zarr = tmp_path / "image.zarr"
    (zarr / "0").mkdir(parents=True)
    (zarr / ".zattrs").write_text('{"multiscales": []}')
    (zarr / ".zgroup").write_text('{"zarr_format": 2}')
    assert formats.sniff(zarr).format == "ome-zarr"  # a reader name SlideTwin does not have
    install(monkeypatch, EntryPoint("zarr", plugin("ome-zarr", ("ome-zarr",))))
    assert route(zarr) == FormatMatch("ome-zarr", "plugin:zarr")
    assert "aperio-svs" in plugins.RESERVED
    assert "kfbio-kfb" not in plugins.RESERVED


def test_read_errors_name_the_slide_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(ArraySlideSource):
        def read_level(self, level: int, x: int, y: int, w: int, h: int) -> Any:
            raise OSError("/secret/path/slide.acme")

    def opener(path: Path, match: FormatMatch, **kw: Any) -> Any:
        return Broken([np.zeros((64, 64, 3), np.uint8)], (0.5, 0.5), name=kw["name"])

    install(monkeypatch, EntryPoint("acme", plugin("acme-slide", opener=opener)))
    source = open_source(
        write(tmp_path, b"ACME"), FormatMatch("acme-slide", "plugin:acme"), name="s1"
    )
    with pytest.raises(SlideReadError, match="by the acme reader plugin \\(OSError\\)") as err:
        source.read_level(0, 0, 0, 8, 8)
    assert "secret" not in err.value.message


def test_ctrl_c_is_never_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*a: Any, **kw: Any) -> Any:
        raise KeyboardInterrupt

    class Reads(ArraySlideSource):
        def read_level(self, level: int, x: int, y: int, w: int, h: int) -> Any:
            raise KeyboardInterrupt

    def opener(path: Path, match: FormatMatch, **kw: Any) -> Any:
        return Reads([np.zeros((64, 64, 3), np.uint8)], (0.5, 0.5), name=kw["name"])

    path = write(tmp_path, b"ACME")
    match = FormatMatch("acme-slide", "plugin:acme")
    install(monkeypatch, EntryPoint("acme", plugin(interrupted)))
    with pytest.raises(KeyboardInterrupt):
        route(path)
    install(monkeypatch, EntryPoint("acme", plugin("acme-slide", opener=interrupted)))
    with pytest.raises(KeyboardInterrupt):
        open_source(path, match, name="s1")
    install(monkeypatch, EntryPoint("acme", plugin("acme-slide", opener=opener)))
    with pytest.raises(KeyboardInterrupt):
        open_source(path, match, name="s1").read_level(0, 0, 0, 8, 8)
