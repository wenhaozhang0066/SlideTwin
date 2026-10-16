"""A stand-in for pyisyntax and its libisyntax calls, for CI, which has no iSyntax sample.

The reader names this module in place of pyisyntax and of its cffi module, so the fake runs in the
reader's worker process, or in the test process when a test calls the adapter itself. A fake slide
(`write_slide`) is an iSyntax XML header that describes the slide (the level sizes, the origin
offsets libisyntax reports, the data model, the pixel size, and how calls behave: raise, crash the
process, hang), then a "seektable" and "codeblocks" of filler bytes. Its level 0 is noise, and
level n holds the 2^n box means of level 0 placed where libisyntax places its levels (`placed`,
measured on real slides), while it reports libisyntax's origin offsets. It reads the file as
libisyntax does: the header in chunks until its end (0x04), then the seektable when it opens, and
codeblocks at every read, so a copy cut short reads short where libisyntax would. Like libisyntax,
it crashes when called from a thread other than the first that called it, and asking for the
label, the macro image or the barcode fails.
"""

from __future__ import annotations

import json
import math
import os
import resource
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from tests.helpers.level_oracle import box_means

CHUNK = 4096  # the header is read in chunks of this size (libisyntax: 1 MiB)
SEEKTABLE = 16  # bytes after the header, read when the slide opens
BLOCK = 64  # bytes after the seektable, read by every read
_FIRST: list[str] = []  # the thread that called first, in this process
NULL = object()


def header(version: str = "5.0", tail: bytes = b"") -> bytes:
    """The start of an iSyntax XML header, as Philips writes it."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\r\n<DataObject ObjectType="DPUfsImport">\r\n'
        b'\t<Attribute Name="PIM_DP_UFS_INTERFACE_VERSION" Group="0x301D" Element="0x1001" '
        b'PMSVR="IString">' + version.encode() + b"</Attribute>\r\n" + tail
    )


def libisyntax_offsets(n_levels: int) -> list[float]:
    """The origin offsets libisyntax 92471db reports: 0, then 3 * 2^(n-1) - 2."""
    return [0.0] + [float(3 * 2 ** (n - 1) - 2) for n in range(1, n_levels)]


def libisyntax_placement(n_levels: int) -> list[float]:
    """Where libisyntax's levels really start on level 0, in level-0 pixels (measured): 0, 0.5,
    then 2^(n-1) + 1.5."""
    return [0.0, 0.5, *(2.0 ** (n - 1) + 1.5 for n in range(2, n_levels))][:n_levels]


def write_slide(
    path: Path, size: tuple[int, int] = (320, 240), levels: int = 4, **spec: Any
) -> Path:
    """A fake slide of `levels` floor halves of `size`. Spec keys override the description: dims,
    offsets (the origin offsets reported), placed (where the levels lie), seed (of level 0's noise),
    scales, model, known, unit (None for none), mpp, fail ('open', 'levels', 'read': raise;
    'crash', 'crash-open': SIGSEGV; 'crash-once': SIGSEGV at a read while `marker` exists; 'hang': a
    read that never ends; 'rgb': reads return RGB, not RGBA)."""
    w, h = size
    description = {
        "dims": [[w >> n, h >> n] for n in range(levels)],
        "offsets": libisyntax_offsets(levels),
        "placed": libisyntax_placement(levels),
        "seed": 3,
        "scales": list(range(levels)),
        "model": 5,
        "known": True,
        "unit": "MicroMeter",
        "mpp": [0.25, 0.25],
        "fail": None,
        **spec,
    }
    fake = b"<Fake>" + json.dumps(description).encode() + b"</Fake>\r\n"
    path.write_bytes(header(tail=fake) + b"\x04" + bytes(SEEKTABLE) + bytes(range(BLOCK)))
    return path


def description(data: bytes) -> dict[str, Any]:
    """The description in a fake slide's header."""
    head = data.partition(b"\x04")[0]
    parsed: dict[str, Any] = json.loads(head.partition(b"<Fake>")[2].partition(b"</Fake>")[0])
    return parsed


def raw_levels(spec: dict[str, Any]) -> list[np.ndarray]:
    """The levels a fake slide serves, in libisyntax's own coordinates (RGB): level 0 is noise,
    and pixel (x, y) of level n is the mean of level 0 over the 2^n x 2^n square from
    (placed[n] + 2^n x, placed[n] + 2^n y), white beyond level 0. A box at a half-pixel position is
    the mean of the four whole-pixel boxes around it."""
    dims = spec["dims"]
    w0, h0 = dims[0] if dims else (0, 0)
    level0 = np.random.default_rng(spec["seed"]).integers(0, 256, (h0, w0, 3)).astype(np.float64)
    out = []
    for n, (w, h) in enumerate(dims):
        s, placed = 2**n, spec["placed"][n]
        lo = math.floor(placed)
        pad = lo + s + 2  # white around level 0, enough for every box
        means = box_means(np.pad(level0, ((pad, pad), (pad, pad), (0, 0)), constant_values=255), s)
        corners = [(0, 0)] if placed == lo else [(0, 0), (1, 0), (0, 1), (1, 1)]
        boxes = [means[pad + lo + dy :: s, pad + lo + dx :: s][:h, :w] for dx, dy in corners]
        out.append(np.rint(sum(boxes) / len(boxes)).astype(np.uint8))
    return out


def levels_of(path: Path) -> list[np.ndarray]:
    """The levels the fake serves for the slide at `path`."""
    return raw_levels(description(path.read_bytes()))


def _crash() -> None:
    """Die as libisyntax does on a damaged file, by SIGSEGV, but leave no core file."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.kill(os.getpid(), signal.SIGSEGV)


def _called() -> None:
    """libisyntax keeps scratch memory per thread: a call from a thread other than the first one
    that called it crashes the process."""
    name = threading.current_thread().name
    if not _FIRST:
        _FIRST.append(name)
    elif name != _FIRST[0]:
        _crash()


def _behave(spec: dict[str, Any], when: str) -> None:
    fail = spec["fail"]
    if fail == when and when in ("open", "levels"):
        raise RuntimeError("not an iSyntax file")
    if fail == when == "read":
        raise OSError("cannot read /data/secret-name.isyntax")
    if (fail, when) in (("crash", "read"), ("crash-open", "open")):
        _crash()
    if (fail, when) == ("crash-once", "read") and Path(spec["marker"]).exists():
        Path(spec["marker"]).unlink()
        _crash()
    if (fail, when) == ("hang", "read"):
        time.sleep(3600)


def _forbidden(*args: Any) -> Any:
    raise AssertionError("SlideTwin asked pyisyntax for the label, the macro image or the barcode")


class Level:
    def __init__(self, slide: ISyntax, n: int) -> None:
        self.ptr = (slide, n)
        self.width, self.height = slide.spec["dims"][n]
        self.scale = slide.spec["scales"][n]


class Image:
    def __init__(self, slide: ISyntax) -> None:
        self._slide = slide

    @property
    def levels(self) -> Iterator[Level]:
        _called()
        _behave(self._slide.spec, "levels")
        for n in range(len(self._slide.spec["dims"])):
            yield Level(self._slide, n)


class ISyntax:
    """Takes the place of isyntax.ISyntax: reads the description from the file it is given."""

    read_label_image_jpeg = read_macro_image_jpeg = _forbidden
    barcode = property(_forbidden)

    def __init__(self, f: Any, n_bytes: int, cache_size: int = 2000) -> None:
        _called()
        if not f.readable():
            raise RuntimeError("IO object must be readable")
        head = b""
        while b"\x04" not in head:
            chunk = bytearray(CHUNK)
            n = f.readinto(chunk)  # the last chunk can end at the end of the file
            if n == 0:
                raise RuntimeError("the XML header does not end")
            head += bytes(chunk[:n])
        end = head.index(b"\x04") + 1
        f.seek(end)
        f.readinto(bytearray(SEEKTABLE))
        self.spec = description(head)
        self.levels = raw_levels(self.spec)
        self.data_offset = end + SEEKTABLE
        self.cache_size = cache_size
        self._f = f
        _behave(self.spec, "open")
        self.ptr = self
        self.mpp_x, self.mpp_y = self.spec["mpp"]

    @property
    def wsi(self) -> Image:
        return Image(self)

    def read_region(self, x: int, y: int, width: int, height: int, level: int = 0) -> np.ndarray:
        _called()
        _behave(self.spec, "read")
        self._f.seek(self.data_offset)
        self._f.readinto(bytearray(BLOCK))  # its codeblocks
        src = self.levels[level]
        out = np.full((height, width, 4), 255, np.uint8)
        x1, y1 = min(x + width, src.shape[1]), min(y + height, src.shape[0])
        x0, y0 = max(x, 0), max(y, 0)
        if x1 > x0 and y1 > y0:
            out[y0 - y : y1 - y, x0 - x : x1 - x, :3] = src[y0:y1, x0:x1]
        return out[:, :, :3] if self.spec["fail"] == "rgb" else out

    def close(self) -> None:
        _called()
        self._f.close()


class Lib:
    """The libisyntax calls the reader makes."""

    def libisyntax_get_is_mpp_known(self, ptr: ISyntax) -> int:
        _called()
        return int(ptr.spec["known"])

    def libisyntax_scale_unit(self, ptr: ISyntax) -> Any:
        _called()
        unit = ptr.spec["unit"]
        return NULL if unit is None else unit.encode()

    def libisyntax_level_get_origin_offset_in_pixels(self, ptr: tuple[ISyntax, int]) -> float:
        _called()
        slide, n = ptr
        return float(slide.spec["offsets"][n])

    def libisyntax_get_data_model_major_version(self, ptr: ISyntax) -> int:
        _called()
        return int(ptr.spec["model"])


class Ffi:
    NULL = NULL

    @staticmethod
    def string(value: bytes) -> bytes:
        return value


lib = Lib()
ffi = Ffi()
