"""Reader plugins (M9): readers a user installs for formats SlideTwin has no reader for.

A plugin is a module (or object) registered in the entry point group "slidetwin.readers":

    # the plugin's pyproject.toml
    [project.entry-points."slidetwin.readers"]
    kfb = "slidetwin_kfb"

It provides:
    FORMATS: the format names it reads (lower-case letters, digits and '-', at most 40);
    sniff(path) -> str | None: one of FORMATS when the file is one it reads, else None;
    open_reader(path, match, *, name, mpp_override, options) -> SlideSource, as the built-in
        readers do (slidetwin.core.slide_source.BaseSource does most of the work).

SlideTwin asks the plugins only about a file none of its own readers serves, in the order of their
entry point names; the first that claims the file reads it, so a plugin never replaces a built-in
reader. For a format the router names (sqray-sdpc, plain-image, ome-zarr, ...) the plugin must
keep that name; for a file the router does not know, it may use any name but those of the formats
SlideTwin reads itself (kfbio-kfb, for example, is free). Two plugins installed under one name are
not used at all. A plugin that fails while sniffing claims nothing; its errors while opening or
reading reach clients as the slide id and the exception type only, since a plugin's own message
may name files. The provenance and the log record the plugin's name, distribution and version,
so a replay notices another plugin. Plugins run with the server's rights: install only those you
trust. The interface is experimental until version 1.0.
"""

from __future__ import annotations

import collections
import importlib.metadata
import re
from pathlib import Path
from typing import Any, cast

from slidetwin.core.errors import SlideMetadataError, SlideReadError
from slidetwin.core.formats import BUILT_IN_FORMATS, NO_READER, FormatMatch
from slidetwin.core.geometry import Point
from slidetwin.core.slide_source import RGB, ReaderOptions, SlideSource, source_provenance

GROUP = "slidetwin.readers"
PREFIX = "plugin:"
_FORMAT = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
# The formats SlideTwin reads itself: a plugin may not give their names to a file the router does
# not know. Those it only names, with no reader yet, are free.
RESERVED = BUILT_IN_FORMATS - set(NO_READER) - {"ome-zarr"}


def entry_points() -> list[Any]:
    return sorted(importlib.metadata.entry_points(group=GROUP), key=lambda ep: ep.name)


def _usable() -> list[Any]:
    """The installed plugins, without names that two of them share (neither is used)."""
    eps = entry_points()
    counts = collections.Counter(ep.name for ep in eps)
    return [ep for ep in eps if counts[ep.name] == 1]


def route(path: Path, match: FormatMatch) -> FormatMatch:
    """`match` (the router's, with no reader that serves the file), or the match of the first
    plugin that claims the file."""
    for ep in _usable():
        fmt = _claim(ep, path, match)
        if fmt is not None:
            return FormatMatch(fmt, PREFIX + ep.name)
    return match


def _claim(ep: Any, path: Path, match: FormatMatch) -> str | None:
    try:
        plugin = ep.load()
        fmt = plugin.sniff(path)
        formats = tuple(plugin.FORMATS)
    except KeyboardInterrupt:
        raise
    except BaseException:  # a broken plugin (even one that calls sys.exit) claims nothing
        return None
    if not (isinstance(fmt, str) and fmt in formats and _FORMAT.fullmatch(fmt)):
        return None
    if match.format != "unknown":
        return fmt if fmt == match.format else None
    return None if fmt in RESERVED else fmt


def open_plugin(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> SlideSource:
    """Open a slide with the plugin its match names."""
    plugin_name = (match.reader or "").removeprefix(PREFIX)
    ep = next((e for e in _usable() if e.name == plugin_name), None)
    if ep is None:
        raise SlideMetadataError(
            f"Slide '{name}' ({match.format}) needs the reader plugin '{plugin_name}', which is "
            "not installed, or installed twice.",
            hint="Install the plugin once, or convert the slide to a pyramidal TIFF.",
        )
    try:
        source = ep.load().open_reader(
            path, match, name=name, mpp_override=mpp_override, options=options
        )
    except KeyboardInterrupt:
        raise
    except BaseException as err:
        raise SlideMetadataError(
            f"Slide '{name}' could not be opened by the {plugin_name} reader plugin "
            f"({type(err).__name__}).",
            hint="The plugin's own message is not shown, since it may name files.",
        ) from None
    dist = getattr(ep, "dist", None)
    origin = (dist.metadata["Name"], dist.version) if dist is not None else ("unknown", "unknown")
    return cast(SlideSource, PluginSource(source, name, plugin_name, match.format, *origin))


class PluginSource:
    """A plugin's slide. SlideTwin, not the plugin, names its format, backend and reader in the
    provenance and the log, and its read errors carry the slide id and exception type only."""

    def __init__(
        self,
        inner: SlideSource,
        name: str,
        plugin: str,
        fmt: str,
        distribution: str,
        version: str,
    ) -> None:
        self._inner = inner
        self._name = name
        self._plugin = plugin
        self.backend = f"{PREFIX}{plugin} {version}"
        own = source_provenance(inner)
        versions = {**own.get("versions", {}), PREFIX + plugin: version}
        self.provenance = {
            **own,
            "reader": PREFIX + plugin,
            "plugin_distribution": distribution,
            "format": fmt,
            "versions": versions,
        }

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        try:
            return self._inner.read_level(level, x, y, w, h)
        except KeyboardInterrupt:
            raise
        except BaseException as err:
            raise SlideReadError(
                f"Slide '{self._name}' could not be decoded at level {level} by the "
                f"{self._plugin} reader plugin ({type(err).__name__}).",
                hint="The plugin's own message is not shown, since it may name files.",
            ) from None

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)
