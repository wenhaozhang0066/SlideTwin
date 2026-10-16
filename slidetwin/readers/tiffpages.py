"""Shared parts of the vendor TIFF readers (M8): Leica SCN, Trestle, Ventana BIF, ARGOS, OME-TIFF
and Akoya QPTIFF.

Each reader picks the pages of one pyramid, and of one focal plane, by the rules of its format.
Labels, macros, overviews and other regions are never read. A level is a tifffile zarr store read
in level coordinates, with tiles that were never written in white. Errors and provenance name the
slide id and the format only, never names from the file.
"""

from __future__ import annotations

import importlib.metadata
import itertools
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
import zarr
from numpy.typing import NDArray

from slidetwin.core.errors import SlideMetadataError, SlideTwinError
from slidetwin.core.geometry import LevelTransform, Point, Size
from slidetwin.core.slide_source import (
    RGB,
    BaseSource,
    Provenance,
    ReaderOptions,
    read_error,
    resolve_mpp,
    to_rgb,
    unreadable_error,
)
from slidetwin.readers.tiff import DECODE_THREADS, FILL, _plausible

_COLOUR = (tifffile.PHOTOMETRIC.RGB, tifffile.PHOTOMETRIC.YCBCR)


class TiffPagesSource(BaseSource):
    """A pyramid read from one tifffile level array per level."""

    def __init__(
        self,
        tif: Any,
        arrays: Sequence[Any],
        dims: Sequence[Size],
        mpp: Point,
        name: str,
        *,
        provenance: Provenance,
        transforms: Sequence[LevelTransform] | None = None,
    ) -> None:
        self._tif = tif
        self._arrays = list(arrays)
        super().__init__(
            dims,
            mpp,
            f"tifffile {tifffile.__version__}",
            name,
            transforms=transforms,
            provenance=provenance,
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        try:
            with self._lock:
                arr = self._read(level, x, y, w, h)
        except Exception as err:
            raise read_error(self._name, level, err) from None
        return to_rgb(arr)

    def _read(self, level: int, x: int, y: int, w: int, h: int) -> NDArray[Any]:
        return np.asarray(self._arrays[level][y : y + h, x : x + w])

    def close(self) -> None:
        self._tif.close()


def open_with[SourceT: TiffPagesSource](
    path: Path, name: str, build: Callable[[Any], SourceT]
) -> SourceT:
    """Open the file and build its source from it; the file is closed again if that fails.
    Unexpected errors name the exception type only."""
    try:
        tif = tifffile.TiffFile(path)
    except Exception as err:
        raise unreadable_error(name, "tifffile", err) from None
    try:
        return build(tif)
    except SlideTwinError:
        tif.close()
        raise
    except Exception as err:
        tif.close()
        raise unreadable_error(name, "tifffile", err) from None


def level_array(page: Any) -> Any:
    """One pyramid page as a zarr array; tiles that were never written read as white."""
    return zarr.open(page.aszarr(fillvalue=FILL[0], maxworkers=DECODE_THREADS), mode="r")


def is_rgb8(page: Any) -> bool:
    """8-bit RGB (or YCbCr) with interleaved samples and one plane: what these readers serve."""
    return bool(
        page.dtype == np.uint8
        and page.axes == "YXS"
        and page.shape[-1] == 3
        and page.photometric in _COLOUR
    )


def proportional(sizes: Sequence[Size]) -> bool:
    """Levels shrink strictly and keep the level-0 proportions, within rounding."""
    w0, h0 = sizes[0]
    ok = True
    for (pw, ph), (w, h) in itertools.pairwise(sizes):
        ok = ok and w < pw and h < ph and abs(h - h0 * w / w0) <= 1.0 + h0 / w0
    return ok


def checked_mpp(
    raw: tuple[float, float] | None, origin: str, override: Point | None, name: str
) -> tuple[Point, str]:
    """The slide's mpp and where it came from: the format's trusted field when plausible
    (0.05-20 um/px), else the manifest's mpp_override; never a guess."""
    if raw is not None and not _plausible(raw):
        raw = None
    x, y = raw if raw is not None else (None, None)
    mpp = resolve_mpp(x, y, override, name)
    return mpp, "mpp_override" if override is not None else origin


def provenance(fmt: str, mpp_source: str, **settings: Any) -> Provenance:
    """Format, mpp source, the reader's settings and the decoder versions (no names from the
    file)."""
    return {
        "format": fmt,
        "mpp_source": mpp_source,
        **settings,
        "fill": list(FILL),
        "versions": {
            "tifffile": tifffile.__version__,
            "imagecodecs": importlib.metadata.version("imagecodecs"),
        },
    }


def refusal(name: str, fmt: str, problem: str, hint: str) -> SlideMetadataError:
    return SlideMetadataError(f"Slide '{name}' ({fmt}) {problem}", hint=hint)


def layout_refusal(name: str, fmt: str) -> SlideMetadataError:
    return refusal(
        name,
        fmt,
        "does not have the layout SlideTwin validated for this format.",
        hint="Convert the slide to a pyramidal TIFF or OME-TIFF with the vendor's software.",
    )


def choose(
    name: str, fmt: str, what: str, value: int | None, count: int, default: int | None = None
) -> int:
    """The scene or focal plane to read, out of `count`. With more than one, the manifest names
    it unless the format has a default."""
    if value is not None:
        chosen = value
    elif count == 1:
        chosen = 0
    elif default is not None:
        chosen = default
    else:
        raise refusal(
            name,
            fmt,
            f"has {count} {what}s; choose one with {what}: 0 to {count - 1}.",
            hint=f"Set {what} in the slide's manifest entry.",
        )
    if not 0 <= chosen < count:
        raise refusal(
            name,
            fmt,
            f"has no {what} {chosen}; it has {count} ({what} 0 to {count - 1}).",
            hint=f"Correct {what} in the slide's manifest entry.",
        )
    return chosen


def require_experimental(name: str, fmt: str, options: ReaderOptions, variant: str) -> None:
    """Variants validated only on synthetic samples need `experimental: true`."""
    if not options.experimental:
        raise refusal(
            name,
            fmt,
            f"is {variant}, which SlideTwin validated only on synthetic samples.",
            hint="Set experimental: true in the slide's manifest entry to serve it anyway.",
        )
