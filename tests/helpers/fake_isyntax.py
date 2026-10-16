"""A stand-in for pyisyntax's library layer, for CI, which has no iSyntax sample.

It serves a pyramid held in memory the way libisyntax places it: level n >= 2 starts one level
pixel late, and the origin offsets are the ones libisyntax reports (1, 4, 10, 22, ... level-0 px).
Every call records its thread, so tests can check that all of them ran on the pyisyntax thread.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from slidetwin.core.slide_source import RGB

NULL = object()


def libisyntax_offsets(n_levels: int) -> list[float]:
    """The origin offsets libisyntax 92471db reports: 0, then 3 * 2^(n-1) - 2."""
    return [0.0] + [float(3 * 2 ** (n - 1) - 2) for n in range(1, n_levels)]


@dataclass
class Fake:
    levels: list[RGB]  # the true pyramid
    offsets: list[float] | None = None
    late: list[int] | None = None  # how many level pixels libisyntax starts late, per level
    model: int = 5
    known: bool = True
    unit: Any = b"MicroMeter"
    mpp: tuple[float, float] = (0.25, 0.25)
    scales: list[int] | None = None
    fail: str | None = None  # "open", "levels" or "read"
    threads: set[str] = field(default_factory=set)
    files: list[Any] = field(default_factory=list)
    closed: int = 0

    def __post_init__(self) -> None:
        n = len(self.levels)
        self.offsets = libisyntax_offsets(n) if self.offsets is None else self.offsets
        self.late = [0, 0, *([1] * (n - 2))][:n] if self.late is None else self.late
        self.scales = list(range(n)) if self.scales is None else self.scales

    def called(self) -> None:
        self.threads.add(threading.current_thread().name)


class Level:
    def __init__(self, fake: Fake, n: int) -> None:
        self.ptr = (fake, n)
        h, w = fake.levels[n].shape[:2]
        self.width, self.height = w, h
        self.scale = fake.scales[n] if fake.scales else n


class Image:
    def __init__(self, fake: Fake) -> None:
        self._fake = fake

    @property
    def levels(self) -> Iterator[Level]:
        self._fake.called()
        if self._fake.fail == "levels":
            raise RuntimeError("cannot list the levels")
        for n in range(len(self._fake.levels)):
            yield Level(self._fake, n)


class ISyntax:
    """Takes the place of isyntax.ISyntax; `current` is the slide it opens."""

    current: Fake

    def __init__(self, f: Any, n_bytes: int, cache_size: int = 2000) -> None:
        self._fake = fake = ISyntax.current
        fake.called()
        fake.files.append(f)
        if fake.fail == "open":
            raise RuntimeError("not an iSyntax file")
        self.ptr = fake
        self.mpp_x, self.mpp_y = fake.mpp

    @property
    def wsi(self) -> Image:
        return Image(self._fake)

    def read_region(self, x: int, y: int, w: int, h: int, level: int) -> np.ndarray:
        fake = self._fake
        fake.called()
        if fake.fail == "read":
            raise OSError("cannot read /data/secret-name.isyntax")
        late = fake.late[level] if fake.late else 0
        src = fake.levels[level]
        out = np.full((h, w, 4), 255, np.uint8)
        x0, y0 = x + late, y + late  # libisyntax's pixel (x, y) is true pixel (x + late, y + late)
        sx0, sy0 = max(x0, 0), max(y0, 0)
        sx1, sy1 = min(x0 + w, src.shape[1]), min(y0 + h, src.shape[0])
        if sx1 > sx0 and sy1 > sy0:
            out[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0, :3] = src[sy0:sy1, sx0:sx1]
        return out

    def close(self) -> None:
        self._fake.called()
        self._fake.closed += 1


class Lib:
    """The libisyntax calls the reader makes."""

    def libisyntax_get_is_mpp_known(self, ptr: Fake) -> int:
        ptr.called()
        return int(ptr.known)

    def libisyntax_scale_unit(self, ptr: Fake) -> Any:
        ptr.called()
        return NULL if ptr.unit is None else ptr.unit

    def libisyntax_level_get_origin_offset_in_pixels(self, ptr: tuple[Fake, int]) -> float:
        fake, n = ptr
        fake.called()
        return fake.offsets[n] if fake.offsets else 0.0

    def libisyntax_get_data_model_major_version(self, ptr: Fake) -> int:
        ptr.called()
        return ptr.model


class Ffi:
    NULL = NULL

    @staticmethod
    def string(value: bytes) -> bytes:
        return value
