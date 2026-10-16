"""Zeiss ZVI (M8): AxioVision images, checked with slideio in a worker process. None is served yet.

A ZVI file is an OLE2 compound document, and so is a Motic MDS slide, which has no open reader. The
directory is read here, without slideio: a ZVI holds an Image storage with a Contents stream and one
Item(n) storage per raw image (one per channel, focal plane, time point and mosaic tile).

The survey tested one ZVI, Zeiss-1-Merged: three fluorescence channels of 14-bit samples in uint16
(slideio returns them as int16), in an ortho-view export with its side panels baked into the
pixels. No brightfield ZVI was tested. So, for now:
- fluorescence and other channel data are refused until SlideTwin renders channels;
- brightfield RGB ZVI is refused as not validated;
- ortho-view exports are refused for good: their pixel size holds for the XY panel only. Their
  metadata does not mark them, but their pixels do: all-zero lines between the panels;
- z-stacks, time series and mosaics are refused as not validated.
The scene name (the acquisition file name), tags and other metadata are never read.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import BinaryIO, NoReturn

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.errors import SlideMetadataError
from slidetwin.core.formats import CONVERT, NO_READER, FormatMatch
from slidetwin.core.geometry import Point
from slidetwin.core.slide_source import ReaderOptions
from slidetwin.readers.slideio_worker import DTYPES, SceneInfo, SlideioWorker

STORAGE, STREAM, ROOT = 1, 2, 5  # OLE2 directory entry types
BAND_ROWS = 256  # rows per read when the pixels are checked
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END, _FREE = 0xFFFFFFFE, 0xFFFFFFFF  # end of a sector chain; no entry
_ITEM = re.compile(r"Image/Item\(\d+\)")
_BITS = {"uint8": "8-bit", "int16": "16-bit", "uint16": "16-bit"}  # int16: see _refuse_pixels
_NOT_YET = "Fluorescence and other channel data in ZVI are not supported yet."


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> NoReturn:
    """Every ZVI is refused so far; the checks say why."""
    options.refuse_unused(name, match.format)
    items = zvi_items(path, name)
    worker = SlideioWorker(path, "ZVI", name)
    try:
        info = _one_image(worker.scenes, items, name)
        if set(info.dtypes) <= DTYPES and has_panels(worker, info):  # samples a worker carries
            raise SlideMetadataError(
                f"Slide '{name}' is an ortho-view export: side panels are part of its pixels, and "
                "its pixel size holds for the XY panel only. SlideTwin does not serve it.",
                hint="Export the plain image, or the z-stack, instead of the ortho view.",
            )
        _refuse_pixels(info, name)
    finally:
        worker.close()


def zvi_items(path: Path, name: str) -> int:
    """The number of raw images in a ZVI file; refuses OLE2 files that are not ZVI."""
    entries = ole2_entries(path, name)
    if entries.get("Image") != STORAGE or entries.get("Image/Contents") != STREAM:
        raise SlideMetadataError(
            f"Slide '{name}' is an OLE2 compound file but not a Zeiss ZVI image (Motic MDS and "
            "MDSX slides are such files).",
            hint=NO_READER["motic-mds"],
        )
    return sum(1 for p, kind in entries.items() if kind == STORAGE and _ITEM.fullmatch(p))


def _one_image(scenes: list[SceneInfo], items: int, name: str) -> SceneInfo:
    """The one image a validated ZVI holds: one plane, one time point, no mosaic tiles."""
    if len(scenes) != 1:
        raise SlideMetadataError(
            f"Slide '{name}' holds {len(scenes)} images where slideio should find one; the file "
            "may be damaged.",
            hint=CONVERT,
        )
    info = scenes[0]
    if info.z != 1 or info.t != 1:
        raise SlideMetadataError(
            f"Slide '{name}' is a ZVI z-stack or time series (z {info.z}, t {info.t}); SlideTwin "
            "has not validated those.",
            hint=CONVERT,
        )
    if items != info.channels:  # one raw image per channel; more are mosaic tiles
        raise SlideMetadataError(
            f"Slide '{name}' is a ZVI mosaic or has an unexpected layout ({items} raw images, "
            f"{info.channels} channel(s)); SlideTwin has not validated those.",
            hint=CONVERT,
        )
    return info


def has_panels(worker: SlideioWorker, info: SceneInfo) -> bool:
    """Whether all-zero rows or columns (every channel) cut the image into panels, as in an
    ortho-view export. Read in bands of rows."""
    w, h = info.size
    rows, cols = np.zeros(h, bool), np.zeros(w, bool)
    for y in range(0, h, BAND_ROWS):
        band = min(BAND_ROWS, h - y)
        lit = (worker.read(0, 0, 0, y, w, band) != 0).reshape(band, w, -1)
        rows[y : y + band] = lit.any(axis=(1, 2))
        cols |= lit.any(axis=(0, 2))
    return split(rows) or split(cols)


def split(lit: NDArray[np.bool_]) -> bool:
    """Whether dark lines lie between lit ones (a frame of dark lines alone does not count)."""
    on = np.flatnonzero(lit)
    return on.size > 0 and not lit[on[0] : on[-1] + 1].all()


def _refuse_pixels(info: SceneInfo, name: str) -> NoReturn:
    """The last check: no kind of ZVI pixels is served yet. slideio reports AxioVision's 16-bit
    samples as int16; they are unsigned (the survey's sample holds 0-16383)."""
    if info.channels == 3 and set(info.dtypes) == {"uint8"}:
        raise SlideMetadataError(
            f"Slide '{name}' is a brightfield colour ZVI image; SlideTwin has not validated ZVI "
            "brightfield images on a sample yet.",
            hint=CONVERT,
        )
    what = "/".join(sorted({_BITS.get(d, d) for d in info.dtypes})) or "no"
    raise SlideMetadataError(
        f"Slide '{name}' is a ZVI image with {info.channels} channel(s) of {what} samples "
        "(fluorescence or monochrome); SlideTwin does not render channel data yet.",
        hint=_NOT_YET,
    )


# --- the OLE2 directory (MS-CFB), read without slideio -----------------------------------------


def ole2_entries(path: Path, name: str) -> dict[str, int]:
    """Storages and streams of an OLE2 compound file by path (for example 'Image/Contents') and
    their entry type. Only the header, the sector table and the directory are read."""
    try:
        with path.open("rb") as f:
            return _directory(f, path.stat().st_size)
    except (OSError, ValueError, struct.error) as err:
        raise SlideMetadataError(
            f"Slide '{name}' has an OLE2 directory that cannot be read ({type(err).__name__}).",
            hint="The slide file may be damaged or incompletely downloaded.",
        ) from None


def _directory(f: BinaryIO, size: int) -> dict[str, int]:
    header = f.read(512)
    if len(header) < 512 or header[:8] != _OLE2:
        raise ValueError("not an OLE2 file")
    (shift,) = struct.unpack_from("<H", header, 30)
    if shift not in (9, 12):
        raise ValueError("unknown sector size")
    sector_size = 1 << shift
    n_fat, first_dir = struct.unpack_from("<II", header, 44)
    first_difat, n_difat = struct.unpack_from("<II", header, 68)
    n_sectors = size // sector_size  # every chain is shorter than this
    per_sector = sector_size // 4

    def sector(s: int) -> bytes:
        if s >= n_sectors:
            raise ValueError("a sector lies outside the file")
        f.seek((s + 1) * sector_size)
        return f.read(sector_size)

    difat = list(struct.unpack_from("<109I", header, 76))
    s = first_difat
    for _ in range(min(n_difat, n_sectors)):
        values = struct.unpack(f"<{per_sector}I", sector(s))
        difat.extend(values[:-1])
        s = values[-1]
    fat = difat[:n_fat]

    def following(s: int) -> int:
        if s // per_sector >= len(fat):
            raise ValueError("a sector lies outside the sector table")
        (value,) = struct.unpack_from("<I", sector(fat[s // per_sector]), (s % per_sector) * 4)
        return int(value)

    raw = b""
    s = first_dir
    for _ in range(n_sectors):
        if s == _END:
            break
        raw += sector(s)
        s = following(s)
    else:
        raise ValueError("the directory does not end")
    return _tree(raw)


def _tree(raw: bytes) -> dict[str, int]:
    """Walk the directory's red-black trees from the root entry; each entry is visited once."""
    count = len(raw) // 128
    if count == 0 or raw[66] != ROOT:
        raise ValueError("no root entry")
    out: dict[str, int] = {}
    seen: set[int] = set()
    pending = [(struct.unpack_from("<I", raw, 76)[0], "")]
    while pending:
        index, prefix = pending.pop()
        if index == _FREE:
            continue
        if index >= count or index in seen:
            raise ValueError("a damaged directory tree")
        seen.add(index)
        entry = raw[index * 128 : (index + 1) * 128]
        (length,) = struct.unpack_from("<H", entry, 64)
        if length > 64 or length % 2:
            raise ValueError("a damaged entry name")
        path = prefix + entry[: max(length - 2, 0)].decode("utf-16-le")
        kind = entry[66]
        out[path] = kind
        left, right, child = struct.unpack_from("<III", entry, 68)
        pending += [(left, prefix), (right, prefix)]
        if kind == STORAGE:
            pending.append((child, path + "/"))
    return out
