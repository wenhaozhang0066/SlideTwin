"""slideio in a worker process (M8), for Olympus VSI and Zeiss ZVI.

slideio crashed the whole process on several formats in the survey (SIGSEGV, SIGFPE, glibc
aborts), and Slide.get_scene_by_name() segfaults. So slideio never runs in the server, only in
workers (slidetwin.readers.worker), and this module is their adapter: in a worker it opens the slide
and describes its scenes. Scenes are addressed by index only. Names, raw metadata and auxiliary
images (label, overview, macro) are never asked for, so they never leave the worker, and slideio's
log is off, because it prints file paths.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from slidetwin.readers.worker import (
    DAMAGED,
    Worker,
    WorkerError,
    floats,
    ints,
    library_version,
    names,
)

LIBRARY = "slideio"
VERSION = library_version(LIBRARY)


@dataclass(frozen=True, slots=True)
class SceneInfo:
    """What a worker says about one scene: numbers and type names, no names from the file."""

    size: tuple[int, int]
    channels: int
    dtypes: tuple[str, ...]
    z: int
    t: int
    resolution: tuple[float, float]  # metres per pixel; 0 when the file has none
    compression: str
    levels: tuple[tuple[int, int], ...]


_FIELDS = frozenset(f.name for f in fields(SceneInfo))


class SlideioWorker(Worker[list[SceneInfo]]):
    """One slide, opened by slideio in its own process."""

    def __init__(self, path: Path, driver: str, name: str) -> None:
        super().__init__(
            path,
            name,
            library="slideio",
            adapter=__name__,
            options={"driver": driver, "library": LIBRARY},
            parse=scene_infos,
        )

    @property
    def scenes(self) -> list[SceneInfo]:
        return self.info

    def read(self, scene: int, level: int, x: int, y: int, w: int, h: int) -> NDArray[Any]:
        """Pixels of level `level` of scene `scene`, (h, w) or (h, w, channels), as slideio
        returns them."""
        request = {"op": "read", "scene": scene, "level": level, "rect": [x, y, w, h]}
        return self.read_block(request, level, (h, w))


def scene_infos(raw: Any) -> list[SceneInfo]:
    """The scenes a worker reports, checked field by field."""
    try:
        return [_scene(item) for item in raw]
    except (TypeError, KeyError, ValueError):
        raise WorkerError(DAMAGED) from None


def _scene(item: dict[str, Any]) -> SceneInfo:
    if set(item) != _FIELDS:
        raise ValueError("unexpected fields")
    channels, z, t = ints([item["channels"], item["z"], item["t"]], 3)
    dtypes = names(item["dtypes"])
    if len(dtypes) != channels:
        raise ValueError("one sample type per channel")
    w, h = ints(item["size"], 2)
    x_res, y_res = floats(item["resolution"], 2)
    return SceneInfo(
        size=(w, h),
        channels=channels,
        dtypes=dtypes,
        z=z,
        t=t,
        resolution=(x_res, y_res),
        compression=names([item["compression"]])[0],
        levels=tuple((lw, lh) for lw, lh in (ints(level, 2) for level in item["levels"])),
    )


# --- in the worker process -----------------------------------------------------------------------


def open_slide(path: str, options: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Open the slide with slideio (or the stand-in that `options` names in tests) and describe
    its scenes. The slide is kept with its scenes, which may need it."""
    library = importlib.import_module(options["library"])
    library.set_log_level("OFF")  # its log names files; keep it out of the server's log
    slide = library.open_slide(path, options["driver"])
    scenes = [slide.get_scene(i) for i in range(slide.num_scenes)]
    return (slide, scenes), [describe(scene) for scene in scenes]


def describe(scene: Any) -> dict[str, Any]:
    """Sizes, sample types, planes, pixel size, codec and levels of a scene."""
    sizes = [scene.get_zoom_level_info(k).size for k in range(scene.num_zoom_levels)]
    return {
        "size": [int(v) for v in scene.size],
        "channels": int(scene.num_channels),
        "dtypes": [
            np.dtype(scene.get_channel_data_type(c)).name for c in range(scene.num_channels)
        ],
        "z": int(scene.num_z_slices),
        "t": int(scene.num_t_frames),
        "resolution": [float(v) for v in scene.resolution],
        "compression": str(scene.compression.name),
        "levels": [[int(s.width), int(s.height)] for s in sizes],
    }


def read(opened: tuple[Any, list[Any]], request: dict[str, Any]) -> Any:
    """A block of a level of a scene, as slideio returns it."""
    x, y, w, h = request["rect"]
    return opened[1][request["scene"]].read_block_from_level(request["level"], (x, y, w, h))
