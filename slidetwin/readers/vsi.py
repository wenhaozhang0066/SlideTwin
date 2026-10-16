"""Olympus VSI (M8): the .vsi file and its _<name>_ folder, read by slideio in a worker process.

A VSI slide is the .vsi file, a TIFF that holds only previews and metadata, and the folder
_<name>_ next to it, whose stack<n>/frame_t.ets files hold the pixels: the acquisition and an
overview of the glass. The .vsi and every .ets file of the stack folders form the slide's file set.
slideio finds no scene, without an error, when the folder is missing, renamed or incomplete, so
such a slide is refused. TIFF readers see only the .vsi previews; the router never gives them one.

One SlideTwin slide per scene, chosen by its index (the manifest's `scene`): a file with several
scenes must name one. Scene names can hold the slide's identifier and are never used; the overview
is an auxiliary image in slideio and is never read. Only 3-channel 8-bit brightfield without focal
planes or time points is served, the variant the survey validated on OS-2. Levels are exact 2^k
reductions of floor size, aligned at the origin (their size ratio is off by up to 0.28%). The pixel
size is the stack calibration as slideio reports it, which the survey found within 2e-8 um of
Bio-Formats.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from slidetwin.core.errors import SlideMetadataError, SlideNotFound, SlideReadError, SlideTwinError
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point
from slidetwin.core.slide_source import RGB, BaseSource, ReaderOptions, resolve_mpp, to_rgb
from slidetwin.readers.slideio_worker import VERSION, SceneInfo, SlideioWorker
from slidetwin.readers.tiff import MPP_RANGE

FILL = (255, 255, 255)
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_KEEP_TOGETHER = (
    "Copy the .vsi file together with its whole _<name>_ folder, and keep both names unchanged."
)


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> VsiSource:
    options.refuse_unused(name, match.format, scene=True)
    return VsiSource(path, name=name, scene=options.scene, mpp_override=mpp_override)


class VsiSource(BaseSource):
    def __init__(
        self,
        path: Path,
        *,
        name: str,
        scene: int | None = None,
        mpp_override: Point | None = None,
    ) -> None:
        fileset = vsi_fileset(path, name)
        worker = SlideioWorker(path, "VSI", name)
        try:
            index, info = _choose_scene(worker.scenes, scene, name)
            _check_pixels(info, name)
            factors = _level_factors(info, name)
            raw = (info.resolution[0] * 1e6, info.resolution[1] * 1e6)  # metres per pixel
            trusted = raw if _plausible(raw) else (None, None)
            mpp = resolve_mpp(trusted[0], trusted[1], mpp_override, name)
        except SlideTwinError:
            worker.close()
            raise
        self._worker = worker
        self._scene = index
        # The files the slide needs: the registry checks and hashes exactly these.
        self.fileset = fileset
        super().__init__(
            info.levels,
            mpp,
            f"slideio {VERSION}",
            name,
            transforms=[LevelTransform(float(f), float(f)) for f in factors],
            provenance={
                "format": "olympus-vsi",
                "scene": index,
                "mpp_source": "mpp_override" if mpp_override else "vsi stack calibration",
                "fill": list(FILL),
                "downsamples": factors,
                "compression": info.compression,
                "versions": {"slideio": VERSION},
            },
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        block = self._worker.read(self._scene, level, x, y, w, h)
        if block.shape != (h, w, 3) or block.dtype != np.uint8:
            raise SlideReadError(
                f"Slide '{self._name}' could not be decoded at level {level} (slideio returned "
                "other samples than it announced)."
            )
        return to_rgb(block)

    def close(self) -> None:
        self._worker.close()


def vsi_fileset(path: Path, name: str) -> tuple[Path, tuple[str, ...]]:
    """The .vsi file and the .ets files of the stack folders of _<stem>_, relative to the .vsi's
    folder (slideio reads _<stem>_/stack<n>/frame_t.ets). Refuses a .vsi that is not a TIFF, and
    one whose folder is missing or holds no stack."""
    with path.open("rb") as f:
        head = f.read(4)
    if head not in _TIFF_MAGIC:
        raise SlideMetadataError(
            f"Slide '{name}' has the extension .vsi but is not an Olympus VSI file.",
            hint="Check that the manifest points to the slide's .vsi file.",
        )
    root, images = path.parent, path.parent / image_folder(path)
    found = sorted(images.glob("stack*/*.ets")) if images.is_dir() else []  # follows symlinks
    files = [p.relative_to(root).as_posix() for p in found if p.is_file()]
    if not any(f.endswith("/frame_t.ets") for f in files):
        raise SlideNotFound(
            f"Slide '{name}' is incomplete: the folder of image files that belongs next to its "
            ".vsi file (_<name>_ with stack folders inside) is missing or empty.",
            hint=_KEEP_TOGETHER,
        )
    _check_names([path.name, *files], name)
    return root, (path.name, *files)


def image_folder(path: Path) -> str:
    """The companion folder's name: _<name of the .vsi file without its extension>_."""
    return f"_{path.stem}_"


def _check_names(files: Iterable[str], name: str) -> None:
    """File names must be printable UTF-8 without backslashes; others are refused, never echoed."""
    for rel in files:
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            ok = False
        else:
            ok = rel.isprintable() and "\\" not in rel
        if not ok:
            raise SlideMetadataError(
                f"Slide '{name}' has a file whose name SlideTwin will not open (not printable "
                "UTF-8).",
                hint="Rename the slide's files, keeping the folder _<name>_ in step with the "
                ".vsi file.",
            )


def _choose_scene(scenes: list[SceneInfo], scene: int | None, name: str) -> tuple[int, SceneInfo]:
    if not scenes:
        raise SlideNotFound(
            f"Slide '{name}' is incomplete or damaged: slideio finds no image in it. A stack "
            "folder of its _<name>_ folder may be missing or renamed.",
            hint=_KEEP_TOGETHER,
        )
    count = len(scenes)
    if scene is None and count > 1:
        raise SlideMetadataError(
            f"Slide '{name}' holds {count} scenes (0-{count - 1}); SlideTwin serves each scene as "
            "its own slide.",
            hint="Add one manifest entry per scene, each with its scene number in `scene`.",
        )
    chosen = 0 if scene is None else scene
    if chosen >= count:
        raise SlideMetadataError(
            f"Slide '{name}' has no scene {chosen}; its scenes are 0-{count - 1}.",
            hint="Correct the scene number in the manifest.",
        )
    return chosen, scenes[chosen]


def _check_pixels(info: SceneInfo, name: str) -> None:
    """Only the validated variant: 3 channels of 8-bit samples, one focal plane, one time point."""
    kinds = set(info.dtypes)
    if info.channels != 3 or kinds != {"uint8"}:
        what = "/".join(sorted(kinds)) or "no"
        raise SlideMetadataError(
            f"Slide '{name}' stores {info.channels}-channel {what} pixels; SlideTwin reads "
            "3-channel 8-bit brightfield VSI so far.",
            hint="Fluorescence and other channel data in VSI are not supported yet.",
        )
    if info.z != 1 or info.t != 1:
        raise SlideMetadataError(
            f"Slide '{name}' is a z-stack or a time series (z {info.z}, t {info.t}); SlideTwin "
            "reads VSI with one focal plane and one time point so far.",
        )


def _level_factors(info: SceneInfo, name: str) -> list[int]:
    """The 2^k of each level: its size is the scene's size over 2^k, rounded down or up."""
    w0, h0 = info.size
    factors: list[int] = []
    for w, h in info.levels:
        k = round(math.log2(w0 / w)) if 0 < w <= w0 else -1
        f = 2**k if k >= 0 else 0
        ok = f > 0 and w in (w0 // f, -(-w0 // f)) and h in (h0 // f, -(-h0 // f))
        if not ok or (factors and f <= factors[-1]):
            factors = []
            break
        factors.append(f)
    if not factors or factors[0] != 1:
        raise SlideMetadataError(
            f"Slide '{name}' has VSI levels that are not 2^k reductions of the scene; SlideTwin "
            "will not guess their scale.",
        )
    return factors


def _plausible(mpp: tuple[float, float]) -> bool:
    lo, hi = MPP_RANGE
    return all(math.isfinite(v) and lo <= v <= hi for v in mpp)
