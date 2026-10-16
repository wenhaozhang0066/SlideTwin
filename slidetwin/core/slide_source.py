"""Slide readers.

Only the image pyramid is exposed. Associated images (label, macro, embedded thumbnail) can carry
patient information and are never read. Reads use level coordinates and must lie inside the level.
"""

from __future__ import annotations

import importlib.metadata
import math
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.errors import (
    InvalidParameter,
    SlideMetadataError,
    SlideNotFound,
    SlideReadError,
    SlideTwinError,
)
from slidetwin.core.geometry import (
    LevelTransform,
    Point,
    Size,
    extent_transforms,
    transform_mpps,
)

RGB = NDArray[np.uint8]
Provenance = dict[str, Any]

BACKENDS = ("tiffslide", "openslide")


@runtime_checkable
class SlideSource(Protocol):
    """Read access to one slide's pyramid."""

    @property
    def dimensions(self) -> Size: ...

    @property
    def mpp(self) -> Point: ...

    @property
    def level_dimensions(self) -> tuple[Size, ...]: ...

    @property
    def backend(self) -> str: ...

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        """RGB uint8 array of shape (h, w, 3) at level coordinates; must lie inside the level."""
        ...

    def close(self) -> None: ...


def slide_size_um(source: SlideSource) -> tuple[float, float]:
    w, h = source.dimensions
    mx, my = source.mpp
    return (w * mx, h * my)


def source_level_transforms(source: SlideSource) -> tuple[LevelTransform, ...]:
    """The source's own level transforms (optional member), else D13's extent ratios."""
    own = getattr(source, "level_transforms", None)
    if own is not None:
        return tuple(own)
    return extent_transforms(source.dimensions, source.level_dimensions)


def source_level_mpps(source: SlideSource) -> tuple[Point, ...]:
    return transform_mpps(source_level_transforms(source), source.mpp)


def source_provenance(source: SlideSource) -> Provenance:
    """What produced the pixels (optional member): reader, format and settings, never names from
    the file. Replay compares everything but `versions`."""
    own = getattr(source, "provenance", None)
    return dict(own) if own is not None else _backend_provenance(source.backend)


def _backend_provenance(backend: str) -> Provenance:
    """Split a backend such as 'tiffslide 4.0.0' into the reader and its version. The version is
    kept apart: a library upgrade that keeps the pixels must not stop a replay (the pixel hashes
    check the pixels)."""
    reader, _, version = backend.partition(" ")
    out: Provenance = {"reader": reader}
    if version:
        out["versions"] = {reader: version}
    return out


def native_mpp(source: SlideSource) -> float:
    """The coarser axis: a magnification is offered only if both axes support it."""
    return max(source.mpp)


def resolve_mpp(raw_x: Any, raw_y: Any, override: Point | None, name: str) -> Point:
    """Validated (mpp_x, mpp_y). Uses the manifest override if given; never guesses."""
    if override is not None:
        values: tuple[Any, Any] = override
        origin = "mpp_override"
    else:
        values = (raw_x, raw_y)
        origin = "slide metadata"
    try:
        mx, my = float(values[0]), float(values[1])
    except (TypeError, ValueError):
        mx = my = math.nan
    if not (math.isfinite(mx) and math.isfinite(my) and mx > 0 and my > 0):
        raise SlideMetadataError(
            f"Slide '{name}' has no usable resolution (mpp) in its {origin}; refusing to guess.",
            hint="If the scanner resolution is known from another source, set mpp_override "
            "{x, y, source} for this slide in configs/slides/manifest.yaml.",
        )
    return (mx, my)


def to_rgb(arr: NDArray[Any]) -> RGB:
    """Convert a decoded tile array to RGB uint8; RGBA is composited over white."""
    if arr.dtype != np.uint8:
        raise SlideMetadataError(f"Unsupported pixel type {arr.dtype}; expected 8-bit samples.")
    if arr.ndim == 2:
        return np.repeat(arr[:, :, None], 3, axis=2)
    if arr.ndim == 3 and arr.shape[2] == 3:
        return np.ascontiguousarray(arr)
    if arr.ndim == 3 and arr.shape[2] == 4:
        rgb = arr[:, :, :3].astype(np.uint32)
        alpha = arr[:, :, 3:4].astype(np.uint32)
        out = (rgb * alpha + 255 * (255 - alpha) + 127) // 255
        return out.astype(np.uint8)
    raise SlideMetadataError(f"Unsupported pixel layout {arr.shape}; expected gray, RGB or RGBA.")


class BaseSource:
    def __init__(
        self,
        level_dimensions: Sequence[Size],
        mpp: Point,
        backend: str,
        name: str,
        *,
        transforms: Sequence[LevelTransform] | None = None,
        provenance: Provenance | None = None,
    ) -> None:
        dims = tuple((int(w), int(h)) for w, h in level_dimensions)
        if not dims or any(w <= 0 or h <= 0 for w, h in dims):
            raise SlideMetadataError(f"Slide '{name}' has an empty or invalid pyramid.")
        self._dims = dims
        self._mpp = mpp
        self._backend = backend
        self._name = name
        self._lock = threading.Lock()
        if transforms is None:
            self._transforms = extent_transforms(dims[0], dims)
        else:
            self._transforms = tuple(transforms)
            first = self._transforms[0] if self._transforms else None
            if len(self._transforms) != len(dims) or first != LevelTransform(1.0, 1.0):
                raise SlideMetadataError(
                    f"Slide '{name}' has level transforms that do not match its pyramid."
                )
        self._provenance: Provenance = {**_backend_provenance(backend), **(provenance or {})}

    @property
    def level_transforms(self) -> tuple[LevelTransform, ...]:
        return self._transforms

    @property
    def provenance(self) -> Provenance:
        return dict(self._provenance)

    @property
    def dimensions(self) -> Size:
        return self._dims[0]

    @property
    def mpp(self) -> Point:
        return self._mpp

    @property
    def level_dimensions(self) -> tuple[Size, ...]:
        return self._dims

    @property
    def backend(self) -> str:
        return self._backend

    def _check(self, level: int, x: int, y: int, w: int, h: int) -> None:
        if not 0 <= level < len(self._dims):
            raise InvalidParameter(f"Level {level} does not exist (0..{len(self._dims) - 1}).")
        lw, lh = self._dims[level]
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > lw or y + h > lh:
            raise InvalidParameter(
                f"Read ({x}, {y}, {w}x{h}) falls outside level {level} ({lw}x{lh} px)."
            )


class ArraySlideSource(BaseSource):
    """In-memory pyramid for tests and synthetic slides."""

    def __init__(
        self,
        levels: Sequence[RGB],
        mpp: Point,
        *,
        name: str = "array",
        transforms: Sequence[LevelTransform] | None = None,
    ) -> None:
        for lvl in levels:
            if lvl.dtype != np.uint8 or lvl.ndim != 3 or lvl.shape[2] != 3:
                raise SlideMetadataError("Array levels must be uint8 arrays of shape (h, w, 3).")
        self._levels = [np.ascontiguousarray(lvl) for lvl in levels]
        super().__init__(
            [(lvl.shape[1], lvl.shape[0]) for lvl in self._levels],
            resolve_mpp(mpp[0], mpp[1], None, name),
            "array",
            name,
            transforms=transforms,
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        return self._levels[level][y : y + h, x : x + w].copy()

    def close(self) -> None:
        self._levels = []


class TiffSlideSource(BaseSource):
    """Default reader (pure Python, via tiffslide)."""

    def __init__(self, path: Path, *, name: str, mpp_override: Point | None = None) -> None:
        import tiffslide

        slide = _open_backend(lambda: tiffslide.TiffSlide(str(path)), name, "tiffslide")
        try:
            props = slide.properties
            mpp = resolve_mpp(
                props.get("tiffslide.mpp-x"), props.get("tiffslide.mpp-y"), mpp_override, name
            )
            group: Any = slide.zarr_group
            self._arrays: list[Any] = [group[str(i)] for i in range(slide.level_count)]
            dims = slide.level_dimensions
        except SlideTwinError:
            slide.close()
            raise
        except Exception as err:
            slide.close()
            raise unreadable_error(name, "tiffslide", err) from None
        self._slide = slide
        backend = f"tiffslide {importlib.metadata.version('tiffslide')}"
        super().__init__(dims, mpp, backend, name)

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        try:
            with self._lock:
                arr = np.asarray(self._arrays[level][y : y + h, x : x + w])
        except Exception as err:
            raise read_error(self._name, level, err) from None
        return to_rgb(arr)

    def close(self) -> None:
        self._slide.close()


class OpenSlideSource(BaseSource):
    """Optional reader (OpenSlide C library via openslide-bin)."""

    def __init__(self, path: Path, *, name: str, mpp_override: Point | None = None) -> None:
        import openslide

        slide = _open_backend(lambda: openslide.OpenSlide(str(path)), name, "openslide")
        try:
            props = slide.properties
            mpp = resolve_mpp(
                props.get(openslide.PROPERTY_NAME_MPP_X),
                props.get(openslide.PROPERTY_NAME_MPP_Y),
                mpp_override,
                name,
            )
            dims = slide.level_dimensions
        except SlideTwinError:
            slide.close()
            raise
        except Exception as err:
            slide.close()
            raise unreadable_error(name, "openslide", err) from None
        self._slide = slide
        version = getattr(openslide, "__library_version__", "unknown")
        super().__init__(dims, mpp, f"openslide {version}", name)

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        ds = self._slide.level_downsamples[level]
        try:
            with self._lock:
                img = self._slide.read_region((round(x * ds), round(y * ds)), level, (w, h))
        except Exception as err:
            raise read_error(self._name, level, err) from None
        return to_rgb(np.asarray(img))

    def close(self) -> None:
        self._slide.close()


def unreadable_error(name: str, backend: str, err: BaseException) -> SlideMetadataError:
    # Only the slide id and the exception type reach clients; file names can carry labels.
    return SlideMetadataError(
        f"Slide '{name}' could not be opened with the {backend} backend ({type(err).__name__}).",
        hint="Check the file, or set backend: openslide for this slide in the manifest.",
    )


def read_error(name: str, level: int, err: BaseException) -> SlideReadError:
    return SlideReadError(
        f"Slide '{name}' could not be decoded at level {level} ({type(err).__name__}).",
        hint="The slide file may be damaged or incompletely downloaded.",
    )


def _open_backend(opener: Any, name: str, backend: str) -> Any:
    try:
        return opener()
    except Exception as err:
        raise unreadable_error(name, backend, err) from None


def open_slide(
    path: Path,
    *,
    name: str = "slide",
    backend: str = "tiffslide",
    mpp_override: Point | None = None,
) -> SlideSource:
    """Open a slide file; refuses files without a usable mpp unless overridden.

    `name` is the public slide id. It is the only identifier that appears in errors: file names
    are internal and may encode labels.
    """
    if backend not in BACKENDS:
        raise InvalidParameter(
            f"Unknown slide backend '{backend}'. Use one of {', '.join(BACKENDS)}."
        )
    if not path.is_file():
        raise SlideNotFound(
            f"The file for slide '{name}' was not found.",
            hint="Check SLIDETWIN_SLIDE_DIR and the path in configs/slides/manifest.yaml.",
        )
    if backend == "tiffslide":
        return TiffSlideSource(path, name=name, mpp_override=mpp_override)
    return OpenSlideSource(path, name=name, mpp_override=mpp_override)
