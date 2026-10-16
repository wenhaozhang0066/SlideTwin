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

SlideTwin asks the plugins only about a file its own router has no reader for, in the order of
their entry point names; the first that claims the file reads it, so a plugin never replaces a
built-in reader. For a format the router knows by name (kfbio-kfb, sqray-sdpc, plain-image, ...)
the plugin must claim that name; for a file the router does not know, any name that is not one of
SlideTwin's own. A plugin that fails while sniffing claims nothing. The provenance names the
plugin and its version, so a replay notices another plugin. Plugins run with the server's rights:
install only those you trust. The interface is experimental until version 1.0.
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path
from typing import Any, cast

from slidetwin.core.errors import SlideMetadataError, SlideTwinError
from slidetwin.core.formats import BUILT_IN_FORMATS, FormatMatch
from slidetwin.core.geometry import Point
from slidetwin.core.slide_source import ReaderOptions, SlideSource, source_provenance
from slidetwin.core.slide_source import unreadable_error as _unreadable

GROUP = "slidetwin.readers"
PREFIX = "plugin:"
_FORMAT = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def entry_points() -> list[Any]:
    return sorted(importlib.metadata.entry_points(group=GROUP), key=lambda ep: ep.name)


def route(path: Path, match: FormatMatch) -> FormatMatch:
    """`match` (the router's, with no reader), or the match of the first plugin that claims
    the file."""
    for ep in entry_points():
        fmt = _claim(ep, path, match)
        if fmt is not None:
            return FormatMatch(fmt, PREFIX + ep.name)
    return match


def _claim(ep: Any, path: Path, match: FormatMatch) -> str | None:
    try:
        plugin = ep.load()
        fmt = plugin.sniff(path)
        formats = tuple(plugin.FORMATS)
    except Exception:  # a broken plugin claims nothing; the file is refused as before
        return None
    if not (isinstance(fmt, str) and fmt in formats and _FORMAT.fullmatch(fmt)):
        return None
    if match.format != "unknown":
        return fmt if fmt == match.format else None
    return None if fmt in BUILT_IN_FORMATS else fmt


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
    ep = next((e for e in entry_points() if e.name == plugin_name), None)
    if ep is None:
        raise SlideMetadataError(
            f"Slide '{name}' ({match.format}) needs the reader plugin '{plugin_name}', which is "
            "not installed.",
            hint="Install the plugin again, or convert the slide to a pyramidal TIFF.",
        )
    try:
        source = ep.load().open_reader(
            path, match, name=name, mpp_override=mpp_override, options=options
        )
    except SlideTwinError:
        raise
    except Exception as err:
        raise _unreadable(name, f"{plugin_name} plugin", err) from None
    version = ep.dist.version if getattr(ep, "dist", None) is not None else "unknown"
    return cast(SlideSource, PluginSource(source, plugin_name, version))


class PluginSource:
    """A plugin's slide; its provenance names the plugin and the plugin's version."""

    def __init__(self, inner: SlideSource, plugin: str, version: str) -> None:
        self._inner = inner
        own = source_provenance(inner)
        versions = {**own.get("versions", {}), PREFIX + plugin: version}
        self.provenance = {**own, "reader": PREFIX + plugin, "versions": versions}

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)
