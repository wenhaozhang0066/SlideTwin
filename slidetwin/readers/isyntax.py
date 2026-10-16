"""Philips iSyntax v1 (M8), read with pyisyntax and the libisyntax it bundles.

libisyntax keeps scratch memory per thread: a call from any thread but the first one that called it
crashes the process (survey). Every pyisyntax call therefore runs on one thread for the whole
process, and the server's threads hand their calls to it and wait. The open handles stay with that
thread, so they are also closed there.

An iSyntax file stores wavelet coefficients, not a pyramid. libisyntax rebuilds level n,
floor(W / 2^n) x floor(H / 2^n) pixels, an exact 2^n reduction of level 0. It places level n >= 2
one level pixel off (an origin offset of 4, 10, 22, ... level-0 pixels), so those levels are read
at x - round(offset / 2^n), the rule of OpenSlide's iSyntax driver (PR #437) that the survey checked
against Philips' own TIFF export. Unscanned areas decode white.

The label, the macro image and the barcode are stored in the XML header and are never read. Of the
header only the interface version is read: iSyntax v2 (100.x) is refused. libisyntax reports a pixel
size of 1.0 when the file has none, so its pixel size is used only when libisyntax says it is known.
libisyntax has no version call; the pyisyntax version pins the libisyntax it bundles.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import itertools
import math
import os
import re
import weakref
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import isyntax  # a missing library is reported with the extra to install
from isyntax._pyisyntax import ffi, lib  # the C calls that the wrapper does not expose
from numpy.typing import NDArray

from slidetwin.core.errors import SlideMetadataError, SlideTwinError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point
from slidetwin.core.slide_source import (
    RGB,
    BaseSource,
    ReaderOptions,
    read_error,
    resolve_mpp,
    to_rgb,
    unreadable_error,
)
from slidetwin.readers.tiff import MPP_RANGE

FILL = (255, 255, 255)
# Decoded tiles kept per open slide. On the survey's test slide a walk of 1280 x 1024 views took a
# median of 29 ms per view at 1000 tiles (about 0.4 GB), 14 ms at 2000 (0.6 GB), 40 ms at 500.
CACHE_TILES = 1000
SERVED_VERSIONS = (5,)  # interface (data model) versions checked on real files: 5.0
HEADER_BYTES = 1 << 16  # the interface version is the header's first attribute
_INTERFACE = re.compile(
    rb'<Attribute\s+Name="PIM_DP_UFS_INTERFACE_VERSION"[^>]*>\s*(\d{1,4})\.(\d{1,4})\s*</Attribute>'
)
_EXPORT = "Export the slide to DICOM or TIFF with Philips' software."

# The one thread that makes every pyisyntax call of this process, and the handles it holds.
_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slidetwin-isyntax")
_HANDLES: dict[int, isyntax.ISyntax] = {}  # touched on _THREAD only
_KEYS = itertools.count(1)


def on_isyntax_thread[T](fn: Callable[..., T], *args: Any) -> T:
    """Run `fn` on the pyisyntax thread and wait for its result."""
    return _THREAD.submit(fn, *args).result()


@dataclass(frozen=True, slots=True)
class _Opened:
    """What libisyntax says about an opened file: numbers only."""

    key: int
    dims: tuple[tuple[int, int], ...]
    scales: tuple[int, ...]  # level n is a 2^scale reduction
    offsets: tuple[float, ...]  # origin offset of each level, level-0 pixels
    model: int  # data model (interface) major version
    mpp: tuple[float, float] | None  # None when libisyntax does not know it


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> ISyntaxSource:
    options.refuse_unused(name, match.format)
    return ISyntaxSource(path, name=name, mpp_override=mpp_override)


class ISyntaxSource(BaseSource):
    def __init__(self, path: Path, *, name: str, mpp_override: Point | None = None) -> None:
        major = interface_version(path, name)
        try:
            opened = on_isyntax_thread(_open, path)
        except Exception as err:
            raise unreadable_error(name, "pyisyntax", err) from None
        self._key = opened.key
        self._finalizer = weakref.finalize(self, _discard, opened.key)
        try:
            shifts = _check_pyramid(opened, major, name)
            raw = opened.mpp
            trusted = raw if raw is not None and _plausible(raw) else (None, None)
            mpp = resolve_mpp(trusted[0], trusted[1], mpp_override, name)
        except SlideTwinError:
            self.close()
            raise
        self._shifts = shifts
        version = importlib.metadata.version("pyisyntax")
        source = "mpp_override" if mpp_override else "isyntax UFS_IMAGE_DIMENSION_SCALE_FACTOR"
        super().__init__(
            opened.dims,
            mpp,
            f"pyisyntax {version}",
            name,
            transforms=[LevelTransform(2.0**k, 2.0**k) for k in opened.scales],
            provenance={
                "format": "philips-isyntax",
                "mpp_source": source,
                "fill": list(FILL),
                "level_shifts": shifts,
                "versions": {"pyisyntax": version},
            },
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        shift = self._shifts[level]
        try:
            rgba = on_isyntax_thread(_read, self._key, level, x - shift, y - shift, w, h)
        except Exception as err:
            raise read_error(self._name, level, err) from None
        return to_rgb(rgba)  # opaque inside the image; anything else is blended over white

    def close(self) -> None:
        if self._finalizer.detach() is not None:
            on_isyntax_thread(_close, self._key)


def interface_version(path: Path, name: str) -> int:
    """The major interface version from the XML header. Refuses iSyntax v2 (100.x), whose open
    reader has known colour and pixel-size errors, and versions SlideTwin has not checked."""
    try:
        with path.open("rb") as f:
            head = f.read(HEADER_BYTES).split(b"\x04", 1)[0]  # 0x04 ends the header
    except OSError as err:
        raise unreadable_error(name, "pyisyntax", err) from None
    found = _INTERFACE.search(head)
    if found is None:
        raise SlideMetadataError(
            f"Slide '{name}' is an iSyntax file without an interface version; SlideTwin will not "
            "guess how to read it.",
            hint=_EXPORT,
        )
    major, minor = int(found[1]), int(found[2])
    if major >= 100:
        raise SlideMetadataError(
            f"Slide '{name}' is Philips iSyntax 2 (interface version {major}.{minor}). SlideTwin "
            "does not read it: no sample has been tested, and its open reader has known colour "
            "and pixel-size errors.",
            hint=_EXPORT,
        )
    if major not in SERVED_VERSIONS:
        raise SlideMetadataError(
            f"Slide '{name}' is iSyntax interface version {major}.{minor}; SlideTwin has checked "
            "version 5 only.",
            hint=_EXPORT,
        )
    return major


def _check_pyramid(opened: _Opened, major: int, name: str) -> list[int]:
    """Exact 2^n levels of floor size; returns the level shifts (level pixels)."""
    if opened.model != major:
        raise SlideMetadataError(
            f"Slide '{name}': libisyntax and the file header disagree on the iSyntax version.",
        )
    w0, h0 = opened.dims[0] if opened.dims else (0, 0)
    exact = opened.scales == tuple(range(len(opened.dims))) and all(
        size == (w0 >> n, h0 >> n) for n, size in enumerate(opened.dims)
    )
    if not exact or w0 <= 0 or h0 <= 0:
        raise SlideMetadataError(
            f"Slide '{name}' has an iSyntax pyramid that is not a series of 2^n reductions.",
        )
    shifts = level_shifts(opened.offsets)
    if shifts is None:
        raise SlideMetadataError(
            f"Slide '{name}' has iSyntax level offsets that SlideTwin has not seen; it will not "
            "guess where the levels lie.",
            hint=_EXPORT,
        )
    return shifts


def level_shifts(offsets: Sequence[float]) -> list[int] | None:
    """round(offset / 2^n) per level, with Python's rounding: level 1's offset is half a pixel and
    gives 0, as the survey found best. None unless level 0 is not offset and every shift is 0 or 1,
    the cases the survey checked."""
    if not offsets or offsets[0] != 0 or not all(math.isfinite(o) and o >= 0 for o in offsets):
        return None
    shifts = [round(offset / 2**n) for n, offset in enumerate(offsets)]
    return shifts if set(shifts) <= {0, 1} else None


def _plausible(mpp: tuple[float, float]) -> bool:
    lo, hi = MPP_RANGE
    return all(math.isfinite(v) and lo <= v <= hi for v in mpp)


# --- calls on the pyisyntax thread ------------------------------------------------------------


def _open(path: Path) -> _Opened:
    f = path.open("rb")
    try:
        handle = isyntax.ISyntax(f, os.fstat(f.fileno()).st_size, cache_size=CACHE_TILES)
    except BaseException:
        f.close()
        raise
    try:
        levels = list(handle.wsi.levels)
        known = bool(lib.libisyntax_get_is_mpp_known(handle.ptr))
        unit = lib.libisyntax_scale_unit(handle.ptr)
        micrometre = unit != ffi.NULL and ffi.string(unit) == b"MicroMeter"
        opened = _Opened(
            key=next(_KEYS),
            dims=tuple((int(lv.width), int(lv.height)) for lv in levels),
            scales=tuple(int(lv.scale) for lv in levels),
            offsets=tuple(
                float(lib.libisyntax_level_get_origin_offset_in_pixels(lv.ptr)) for lv in levels
            ),
            model=int(lib.libisyntax_get_data_model_major_version(handle.ptr)),
            mpp=(float(handle.mpp_x), float(handle.mpp_y)) if known and micrometre else None,
        )
    except BaseException:
        handle.close()
        raise
    _HANDLES[opened.key] = handle
    return opened


def _read(key: int, level: int, x: int, y: int, w: int, h: int) -> NDArray[Any]:
    return _HANDLES[key].read_region(x, y, w, h, level)


def _close(key: int) -> None:
    _HANDLES.pop(key).close()  # once per handle: close() and the finalizer exclude each other


def _discard(key: int) -> None:
    """Close the handle of a source that was dropped unclosed. The close is queued, not awaited:
    this can run on the pyisyntax thread itself. During interpreter shutdown nothing can be queued
    (RuntimeError), and the handle goes with the process."""
    with contextlib.suppress(RuntimeError):
        _THREAD.submit(_close, key)
