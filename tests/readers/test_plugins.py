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
from slidetwin.core.errors import SlideMetadataError
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
        return None if self.version is None else SimpleNamespace(version=self.version)


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

    install(monkeypatch, EntryPoint("acme", plugin("acme-slide", opener=damaged)))
    with pytest.raises(SlideMetadataError, match="with the acme plugin backend") as err:
        open_source(path, match, name="s1")
    assert "secret" not in err.value.message
    install(monkeypatch, EntryPoint("acme", plugin("acme-slide", opener=refuses)))
    with pytest.raises(SlideMetadataError, match="is a z-stack"):
        open_source(path, match, name="s1")
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
