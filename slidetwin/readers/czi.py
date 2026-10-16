"""Zeiss CZI brightfield slides (M8): czifile decodes the subblocks, SlideTwin places them.

One SlideTwin slide per scene (the manifest's `scene`, the scene index stored in the file). A file
with several scenes needs that choice: serving one of them silently would hide the others from the
reader and from the coverage check. The rules follow the survey, where this compositor matched
Zeiss libCZI and Bio-Formats on Zeiss-5-JXR and Zeiss-5-Cropped:

- Level k is the stored layer at scale 2^k, floor(W0 / 2^k) x floor(H0 / 2^k) pixels, where
  W0 x H0 is the extent of the scene's level-0 subblocks. A subblock sits at
  floor((start - scene origin) / 2^k).
- Overlapping camera frames are drawn in ascending mosaic index (M), so the highest is on top, as
  ZEN draws them. Pixels outside a subblock's valid-pixel mask are not drawn.
- Unscanned areas are white.
- czifile turns the stored BGR samples into RGB. 16-bit brightfield is shifted right by the file's
  bit count (ComponentBitCount) minus 8.

Only single-file brightfield RGB (Bgr24, Bgr48) is served so far. Labels, slide previews and
thumbnails are attachments and are never read; of the XML metadata only the pixel size and the
bit count are read.
"""

from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import czifile
import numpy as np
from czifile import CziCompressionType, CziPixelType
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
    unreadable_error,
)
from slidetwin.readers.tiff import MPP_RANGE

FILL = (255, 255, 255)
# Decoded subblocks kept per slide (czifile's cache). On the Axioscan samples a 1280 x 1024 view
# touches at most four camera frames of 2056 x 2464 px (about 15 MB each), so this holds the
# current view and its neighbours; without it a view takes about a second (survey).
CACHE_SUBBLOCKS = 16
# Compressions checked on real slides (survey: JPEG XR, zstd, zstd with HiLo byte packing), and
# on synthetic files only (served with experimental: true).
SERVED = frozenset({CziCompressionType.JPEGXR, CziCompressionType.ZSTD, CziCompressionType.ZSTDHDR})
EXPERIMENTAL = frozenset({CziCompressionType.UNCOMPRESSED})
BRIGHTFIELD = {CziPixelType.BGR24: 8, CziPixelType.BGR48: 16}  # pixel type -> bits per sample
_PLANES = {
    "C": "several channels (fluorescence or multichannel)",
    "Z": "several focal planes (a z-stack)",
    "T": "several time points",
}
_IMAGE_BITS = "Metadata/Information/Image/ComponentBitCount"
_CHANNEL_BITS = "Metadata/Information/Image/Dimensions/Channels/Channel/ComponentBitCount"


@dataclass(frozen=True, slots=True)
class Tile:
    """A stored subblock placed on its level, in level pixels from the scene origin."""

    x: int
    y: int
    w: int
    h: int
    order: tuple[int, int]  # (mosaic index, file position): drawing order
    entry: Any = None


Loader = Callable[[Tile], tuple[NDArray[Any], NDArray[np.bool_] | None]]


class Layer:
    """The tiles of one level in drawing order, with their boxes for quick lookup."""

    def __init__(self, tiles: Iterable[Tile]) -> None:
        self.tiles = sorted(tiles, key=lambda t: t.order)
        boxes = [(t.x, t.y, t.x + t.w, t.y + t.h) for t in self.tiles]
        self._boxes = np.array(boxes, np.int64).reshape(-1, 4)

    def overlapping(self, x: int, y: int, w: int, h: int) -> list[Tile]:
        b = self._boxes
        hit = (b[:, 0] < x + w) & (b[:, 2] > x) & (b[:, 1] < y + h) & (b[:, 3] > y)
        return [self.tiles[i] for i in np.flatnonzero(hit)]


def compose(
    tiles: Iterable[Tile], x: int, y: int, w: int, h: int, load: Loader, shift: int = 0
) -> RGB:
    """Draw `tiles` in order onto a white (h, w) canvas whose top-left is level pixel (x, y).

    `load` gives a tile's decoded RGB samples and its valid-pixel mask (or None). Samples wider
    than 8 bits are shifted right by `shift`.
    """
    out = np.empty((h, w, 3), np.uint8)
    out[...] = FILL
    for tile in tiles:
        data, mask = load(tile)
        th, tw = data.shape[:2]  # the decoded size can be a pixel short of the directory's
        x0, y0 = max(tile.x, x), max(tile.y, y)
        x1, y1 = min(tile.x + tw, x + w), min(tile.y + th, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        src = data[y0 - tile.y : y1 - tile.y, x0 - tile.x : x1 - tile.x]
        if src.dtype != np.uint8:
            src = np.minimum(src >> shift, 255).astype(np.uint8)
        dst = out[y0 - y : y1 - y, x0 - x : x1 - x]
        if mask is None or mask.shape != (th, tw):
            dst[...] = src
        else:
            valid = mask[y0 - tile.y : y1 - tile.y, x0 - tile.x : x1 - tile.x]
            dst[valid] = src[valid]
    return out


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> CziSource:
    options.refuse_unused(name, match.format, scene=True)
    return CziSource(
        path,
        name=name,
        scene=options.scene,
        mpp_override=mpp_override,
        experimental=options.experimental,
    )


class CziSource(BaseSource):
    def __init__(
        self,
        path: Path,
        *,
        name: str,
        scene: int | None = None,
        mpp_override: Point | None = None,
        experimental: bool = False,
    ) -> None:
        try:
            czi = czifile.CziFile(path)
        except Exception as err:
            raise unreadable_error(name, "czifile", err) from None
        try:
            if czi.header.file_part != 0:
                raise _multi_file(name)
            scene, entries = _scene_entries(czi.subblock_directory, scene, name)
            pixel_type = _check_entries(entries, name, experimental)
            levels = _levels(entries, name)
            root = czi.xml_element
            shift = _bit_shift(pixel_type, root, name)
            raw = _scaling_mpp(root)
            trusted = raw if raw is not None and _plausible(raw) else (None, None)
            mpp = resolve_mpp(trusted[0], trusted[1], mpp_override, name)
        except SlideTwinError:
            czi.close()
            raise
        except Exception as err:
            czi.close()
            raise unreadable_error(name, "czifile", err) from None
        czi.maxcache = CACHE_SUBBLOCKS
        self._czi = czi
        self._shift = shift
        self._layers = [layer for _, _, layer in levels]
        version = czifile.__version__
        super().__init__(
            [size for _, size, _ in levels],
            mpp,
            f"czifile {version}",
            name,
            transforms=[LevelTransform(2.0**k, 2.0**k) for k, _, _ in levels],
            provenance={
                "format": "zeiss-czi",
                "scene": scene,
                "mpp_source": "mpp_override" if mpp_override else "czi Scaling Distance",
                "fill": list(FILL),
                "bit_shift": shift,
                "versions": {
                    "czifile": version,
                    "imagecodecs": importlib.metadata.version("imagecodecs"),
                },
            },
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        try:
            with self._lock:
                tiles = self._layers[level].overlapping(x, y, w, h)
                return compose(tiles, x, y, w, h, self._load, self._shift)
        except Exception as err:
            raise read_error(self._name, level, err) from None

    def _load(self, tile: Tile) -> tuple[NDArray[Any], NDArray[np.bool_] | None]:
        segment = tile.entry.read_segment_data(self._czi)
        data = np.asarray(segment.data())
        return data.reshape(data.shape[-3:]), segment.mask()

    def close(self) -> None:
        self._czi.close()


def _multi_file(name: str) -> SlideMetadataError:
    return SlideMetadataError(
        f"Slide '{name}' is part of a multi-file CZI, which SlideTwin does not read.",
        hint="Save the image as a single CZI file in ZEN.",
    )


def _scene_entries(entries: Sequence[Any], scene: int | None, name: str) -> tuple[int, list[Any]]:
    """The chosen scene's subblocks. A file without scene indices is one scene, numbered 0."""
    scenes: dict[int, list[Any]] = {}
    for entry in entries:
        scenes.setdefault(entry.scene_index, []).append(entry)
    if -1 in scenes and len(scenes) > 1:
        raise SlideMetadataError(f"Slide '{name}' has subblocks that belong to no scene.")
    if -1 in scenes:
        scenes = {0: scenes[-1]}
    if not scenes:
        raise SlideMetadataError(f"Slide '{name}' holds no image subblocks.")
    available = ", ".join(str(s) for s in sorted(scenes))
    if scene is None and len(scenes) > 1:
        raise SlideMetadataError(
            f"Slide '{name}' holds {len(scenes)} scenes ({available}); SlideTwin serves each "
            "scene as its own slide.",
            hint="Add one manifest entry per scene, each with its scene number in `scene`.",
        )
    chosen = next(iter(scenes)) if scene is None else scene
    if chosen not in scenes:
        raise SlideMetadataError(
            f"Slide '{name}' has no scene {chosen}; its scenes are {available}.",
            hint="Correct the scene number in the manifest.",
        )
    return chosen, scenes[chosen]


def _check_entries(entries: list[Any], name: str, experimental: bool) -> Any:
    """Refuse anything but single-plane brightfield RGB in a checked compression; return the
    pixel type."""
    dims = entries[0].dims
    if dims[-3:] != ("Y", "X", "S") or any(entry.dims != dims for entry in entries):
        raise SlideMetadataError(f"Slide '{name}' has an unexpected CZI subblock layout.")
    if any(entry.file_part != 0 for entry in entries):
        raise _multi_file(name)
    pixel_types = {entry.pixel_type for entry in entries}
    if len(pixel_types) != 1 or not pixel_types <= BRIGHTFIELD.keys():
        kinds = ", ".join(sorted(str(p.name) for p in pixel_types))
        raise SlideMetadataError(
            f"Slide '{name}' stores {kinds} pixels; SlideTwin reads brightfield RGB CZI "
            "(Bgr24, Bgr48) so far.",
            hint="Fluorescence and other channel data in CZI are not supported yet.",
        )
    for compression in sorted({entry.compression for entry in entries}):
        if compression in SERVED or (experimental and compression in EXPERIMENTAL):
            continue
        if compression in EXPERIMENTAL:
            raise SlideMetadataError(
                f"Slide '{name}' uses {compression.name} compression, which SlideTwin has "
                "checked on synthetic files only.",
                hint="Set experimental: true for this slide in the manifest to serve it anyway.",
            )
        raise SlideMetadataError(
            f"Slide '{name}' uses {compression.name} compression, which SlideTwin does not read.",
            hint="Save the image with JPEG XR or zstd compression in ZEN.",
        )
    for i, dim in enumerate(dims[:-3]):
        starts = {entry.start[i] for entry in entries}
        sizes = {entry.shape[i] for entry in entries}
        if len(starts) > 1 or sizes != {1}:
            what = _PLANES.get(dim, f"several planes along dimension {dim}")
            raise SlideMetadataError(
                f"Slide '{name}' has {what}; SlideTwin reads single-plane brightfield CZI so far.",
            )
    return next(iter(pixel_types))


def _levels(entries: list[Any], name: str) -> list[tuple[int, tuple[int, int], Layer]]:
    """(k, level size, layer) per stored pyramid layer, finest first."""
    dims = entries[0].dims
    xi, yi = dims.index("X"), dims.index("Y")
    by_k: dict[int, list[Any]] = {}
    for entry in entries:
        fx, fy = entry.scale[xi], entry.scale[yi]
        if fx != fy or not isinstance(fx, int) or fx < 1 or fx & (fx - 1):
            raise SlideMetadataError(
                f"Slide '{name}' has a pyramid layer whose scale is not a power of two.",
            )
        by_k.setdefault(fx.bit_length() - 1, []).append(entry)
    if 0 not in by_k:
        raise SlideMetadataError(f"Slide '{name}' has no full-resolution subblocks.")
    base = by_k[0]
    x0 = min(e.start[xi] for e in base)
    y0 = min(e.start[yi] for e in base)
    w0 = max(e.stop[xi] for e in base) - x0
    h0 = max(e.stop[yi] for e in base) - y0
    out = []
    for k in sorted(by_k):
        f = 2**k
        size = (w0 // f, h0 // f)
        if 0 in size:
            continue  # a layer coarser than one pixel
        tiles = [
            Tile(
                (e.start[xi] - x0) // f,
                (e.start[yi] - y0) // f,
                e.stored_shape[xi],
                e.stored_shape[yi],
                (e.mosaic_index, e.file_position),
                e,
            )
            for e in by_k[k]
        ]
        out.append((k, size, Layer(tiles)))
    return out


def _bit_shift(pixel_type: Any, root: Any, name: str) -> int:
    """How far 16-bit samples are shifted right: the file's bit count minus 8."""
    if BRIGHTFIELD[pixel_type] == 8:
        return 0
    text = None
    if root is not None:
        text = root.findtext(_IMAGE_BITS) or root.findtext(_CHANNEL_BITS)
    bits = _integer(text) if text else None
    if bits is None or not 8 <= bits <= 16:
        raise SlideMetadataError(
            f"Slide '{name}' stores 16-bit samples without a usable bit count; SlideTwin will "
            "not guess how to scale them.",
        )
    return bits - 8


def _scaling_mpp(root: Any) -> tuple[float, float] | None:
    """Pixel size in um from Scaling/Items/Distance (metres per pixel), or None."""
    if root is None:
        return None
    values = []
    for axis in ("X", "Y"):
        text = root.findtext(f"Metadata/Scaling/Items/Distance[@Id='{axis}']/Value")
        try:
            values.append(float(text) * 1e6)
        except (TypeError, ValueError):
            return None
    return (values[0], values[1])


def _integer(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _plausible(mpp: tuple[float, float]) -> bool:
    lo, hi = MPP_RANGE
    return all(math.isfinite(v) and lo <= v <= hi for v in mpp)
