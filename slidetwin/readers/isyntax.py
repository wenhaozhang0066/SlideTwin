"""Philips iSyntax v1 (M8), read with pyisyntax and the libisyntax it bundles, in a worker process.

libisyntax takes its process down on damaged files: copies cut short crashed it (SIGSEGV) or made a
read loop forever (review r04), and a call from any thread but the first one that called it crashes
it too (survey). So pyisyntax never runs in the server, only in workers (slidetwin.readers.worker),
one process of one thread per open slide, and this module is also their adapter. A crash or a hang
then fails one read of one slide, and the next read starts a new worker.

libisyntax does not check how much a read of the file returned (pyisyntax reports at least one
byte), so a read past the end of a truncated copy would decode whatever its buffer held. It reads
the XML header in chunks, the last of which may run past the end of the file; every read after the
header's end (byte 0x04) asks for data the file lists: its seektable at open, then codeblocks. So
from that byte on, a read that comes back short means the copy ends early, and the worker ends at
once: the open or the read fails with that reason. A copy cut in its seektable is thus refused at
open; one cut in its codeblocks fails the reads that need the missing part. libisyntax does not
expose the seektable, so the file size is not checked against it in advance.

An iSyntax file stores wavelet coefficients, not a pyramid. libisyntax rebuilds level n,
floor(W / 2^n) x floor(H / 2^n) pixels, an exact 2^n reduction of level 0, but off level 0's grid:
its pixel 0 starts 0.5 level-0 pixels in at level 1 and 2^(n-1) + 1.5 at level n >= 2 (3.5, 5.5,
9.5, ...). These are the levels' transform offsets, and the renderer resamples at them. They were
measured by matching each level with the 2^n box means of level 0: on four samples the median over
12 tissue windows lay within 0.06 pixel of them at levels 1-3 and within 0.25 at levels 4-5.
libisyntax reports other origin offsets (0, 1, 4, 10, 22, ...); the rule of OpenSlide's iSyntax
driver (PR #437) built on them, reading level n >= 2 at x - round(offset / 2^n), put level 1 a
quarter of a level pixel off and levels n >= 3 up to half a level pixel (review). A slide for which
libisyntax reports other origin offsets is refused. Unscanned areas decode white.

The label, the macro image and the barcode are stored in the XML header and are never read. Of the
header only the interface version is read: iSyntax v2 (100.x) is refused. libisyntax reports a pixel
size of 1.0 when the file has none, so its pixel size is used only when libisyntax says it is known.
libisyntax has no version call; the pyisyntax version pins the libisyntax it bundles.
"""

from __future__ import annotations

import importlib
import io
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from slidetwin.core.errors import SlideMetadataError, SlideReadError, SlideTwinError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point
from slidetwin.core.slide_source import (
    RGB,
    BaseSource,
    ReaderOptions,
    resolve_mpp,
    to_rgb,
    unreadable_error,
)
from slidetwin.readers.worker import (
    DAMAGED,
    TRUNCATED,
    Worker,
    WorkerError,
    floats,
    ints,
    library_version,
)

LIBRARY = "isyntax"  # pyisyntax's module (tests name a stand-in)
LOWLEVEL = "isyntax._pyisyntax"  # its cffi module: the libisyntax calls the wrapper does not expose
VERSION = library_version(LIBRARY, "pyisyntax")
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
_REPORT = frozenset({"dims", "scales", "offsets", "model", "mpp"})


@dataclass(frozen=True, slots=True)
class _Opened:
    """What libisyntax says about an opened file: numbers only."""

    dims: tuple[tuple[int, int], ...]
    scales: tuple[int, ...]  # level n is a 2^scale reduction
    offsets: tuple[float, ...]  # the origin offsets libisyntax reports (not where levels lie)
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
        worker = Worker(
            path,
            name,
            library="pyisyntax",
            adapter=__name__,
            options={"library": LIBRARY, "lowlevel": LOWLEVEL, "cache_tiles": CACHE_TILES},
            parse=opened,
        )
        info = worker.info
        try:
            offsets = _check_pyramid(info, major, name)
            raw = info.mpp
            trusted = raw if raw is not None and _plausible(raw) else (None, None)
            mpp = resolve_mpp(trusted[0], trusted[1], mpp_override, name)
        except SlideTwinError:
            worker.close()
            raise
        self._worker = worker
        source = "mpp_override" if mpp_override else "isyntax UFS_IMAGE_DIMENSION_SCALE_FACTOR"
        super().__init__(
            info.dims,
            mpp,
            f"pyisyntax {VERSION}",
            name,
            transforms=[
                LevelTransform(2.0**k, 2.0**k, o, o)
                for k, o in zip(info.scales, offsets, strict=True)
            ],
            provenance={
                "format": "philips-isyntax",
                "mpp_source": source,
                "fill": list(FILL),
                "level_offsets": offsets,  # level-0 px
                "versions": {"pyisyntax": VERSION},
            },
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        request = {"op": "read", "level": level, "rect": [x, y, w, h]}
        rgba = self._worker.read_block(request, level, (h, w))
        if rgba.shape != (h, w, 4) or rgba.dtype != np.uint8:
            raise SlideReadError(
                f"Slide '{self._name}' could not be decoded at level {level} (pyisyntax returned "
                "other samples than RGBA)."
            )
        return to_rgb(rgba)  # opaque inside the image; anything else is blended over white

    def close(self) -> None:
        self._worker.close()


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


def opened(raw: Any) -> _Opened:
    """What a worker reports about the file it opened, checked field by field."""
    try:
        if set(raw) != _REPORT:
            raise ValueError("unexpected fields")
        dims = tuple((w, h) for w, h in (ints(size, 2) for size in raw["dims"]))
        mpp = None if raw["mpp"] is None else floats(raw["mpp"], 2)
        return _Opened(
            dims=dims,
            scales=ints(raw["scales"], len(dims)),
            offsets=floats(raw["offsets"], len(dims)),
            model=ints([raw["model"]], 1)[0],
            mpp=None if mpp is None else (mpp[0], mpp[1]),
        )
    except (TypeError, KeyError, ValueError):
        raise WorkerError(DAMAGED) from None


def _check_pyramid(opened: _Opened, major: int, name: str) -> list[float]:
    """Exact 2^n levels of floor size; returns where they start on level 0 (level-0 pixels)."""
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
    offsets = level_offsets(opened.offsets)
    if offsets is None:
        raise SlideMetadataError(
            f"Slide '{name}' has iSyntax level offsets that SlideTwin has not seen; it will not "
            "guess where the levels lie.",
            hint=_EXPORT,
        )
    return offsets


def level_offsets(origins: Sequence[float]) -> list[float] | None:
    """Where libisyntax's levels start on level 0 (level-0 pixels): 0, 0.5, then 2^(n-1) + 1.5,
    as measured. Only for the origin offsets that libisyntax reported on the measured samples,
    0 then 3 * 2^(n-1) - 2; None for any others."""
    if list(origins) != [0.0] + [3.0 * 2 ** (n - 1) - 2 for n in range(1, len(origins))]:
        return None
    return [0.0, 0.5, *(2.0 ** (n - 1) + 1.5 for n in range(2, len(origins)))][: len(origins)]


def _plausible(mpp: tuple[float, float]) -> bool:
    from slidetwin.readers.tiff import MPP_RANGE  # not at the top: the worker needs no TIFF reader

    lo, hi = MPP_RANGE
    return all(math.isfinite(v) and lo <= v <= hi for v in mpp)


# --- in the worker process -----------------------------------------------------------------------


class _Whole:
    """The slide file as libisyntax reads it, in the worker. After the read that holds the end of
    the XML header, a read that returns less than it asked for ends the worker at once with exit
    status TRUNCATED: the file ends before data that it lists, and libisyntax would decode the rest
    of its buffer as if it had been read."""

    def __init__(self, f: io.BufferedReader) -> None:
        self._f = f
        self._listed = False  # whether reads now ask only for data the file lists

    def readable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._f.seek(offset, whence)

    def readinto(self, buffer: Any) -> int:
        n = self._f.readinto(buffer)
        if self._listed:
            if n < len(buffer):
                os._exit(TRUNCATED)
        elif b"\x04" in memoryview(buffer)[:n].tobytes():  # XML has no 0x04: the header ends
            self._listed = True
        return n

    def close(self) -> None:
        self._f.close()


def open_slide(path: str, options: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Open the file with pyisyntax (or the stand-in that `options` names in tests), keeping
    `cache_tiles` decoded tiles, and report its levels, their origin offsets, the data model and
    the pixel size when libisyntax knows it."""
    library = importlib.import_module(options["library"])
    lowlevel = importlib.import_module(options["lowlevel"])
    ffi, lib = lowlevel.ffi, lowlevel.lib
    file = Path(path)
    f = file.open("rb")  # libisyntax reads it for as long as the slide is open
    try:
        size = os.fstat(f.fileno()).st_size
        handle = library.ISyntax(_Whole(f), size, cache_size=options["cache_tiles"])
    except BaseException:
        f.close()
        raise
    levels = list(handle.wsi.levels)
    known = bool(lib.libisyntax_get_is_mpp_known(handle.ptr))
    unit = lib.libisyntax_scale_unit(handle.ptr)
    micrometre = unit != ffi.NULL and ffi.string(unit) == b"MicroMeter"
    return handle, {
        "dims": [[int(lv.width), int(lv.height)] for lv in levels],
        "scales": [int(lv.scale) for lv in levels],
        "offsets": [
            float(lib.libisyntax_level_get_origin_offset_in_pixels(lv.ptr)) for lv in levels
        ],
        "model": int(lib.libisyntax_get_data_model_major_version(handle.ptr)),
        "mpp": [float(handle.mpp_x), float(handle.mpp_y)] if known and micrometre else None,
    }


def read(handle: Any, request: dict[str, Any]) -> Any:
    """RGBA pixels of a region of a level, in libisyntax's level coordinates."""
    x, y, w, h = request["rect"]
    return handle.read_region(x, y, w, h, request["level"])
