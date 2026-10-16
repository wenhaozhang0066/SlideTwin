"""A stand-in for slideio, loaded by the slideio worker in tests (CI has no VSI or ZVI sample).

open_slide(path, driver) reads what to be from `<path>.fake.json` (see `write_spec`): the scenes,
and how opening and reading behave (ok, raise, crash the process, hang, return wrong samples).
Pixels are a fixed function of their position (`pixels`), so tests know what a read must return.
Every slideio call that SlideTwin must never make (names, raw metadata, auxiliary images, lookup by
name) raises.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


def scene_spec(
    size: tuple[int, int] = (1000, 600),
    levels: int = 3,
    *,
    channels: int = 3,
    dtype: str = "uint8",
    z: int = 1,
    t: int = 1,
    resolution: tuple[float, float] = (0.5e-6, 0.5e-6),
    compression: str = "Jpeg2000",
    layout: str = "plain",
) -> dict[str, Any]:
    w, h = size
    return {
        "size": [w, h],
        "levels": [[w >> k, h >> k] for k in range(levels)],
        "channels": channels,
        "dtype": dtype,
        "z": z,
        "t": t,
        "resolution": list(resolution),
        "compression": compression,
        "layout": layout,
    }


def write_spec(slide: Path, scenes: list[dict[str, Any]], **behaviour: Any) -> Path:
    """The fake's description of `slide`. Behaviour keys: open, read ('ok', 'error', 'crash',
    'hang', and for read also 'crash-once', 'shape', 'dtype', 'gray'), marker (crash-once's file),
    driver (the only driver that opens it)."""
    spec = slide.with_name(slide.name + ".fake.json")
    spec.write_text(json.dumps({"scenes": scenes, **behaviour}))
    return spec


def pixels(
    scene: int, level: int, x: int, y: int, w: int, h: int, channels: int = 3, dtype: str = "uint8"
) -> np.ndarray:
    """What the fake returns for a read; a 2-D array for one channel."""
    yy, xx = np.mgrid[y : y + h, x : x + w]
    planes = [(xx * 3 + yy * 5 + level * 17 + c * 80 + scene * 40) % 251 for c in range(channels)]
    data = np.stack(planes, axis=-1).astype(dtype)
    return data[:, :, 0] if channels == 1 else data


def ortho(data: np.ndarray, frame: int = 3, at: int = 20) -> np.ndarray:
    """An ortho-view export's layout: a black frame and black lines between the panels."""
    out = data.copy()
    out[:frame], out[-frame:], out[:, :frame], out[:, -frame:] = 0, 0, 0, 0
    out[at : at + 2], out[:, -at - 2 : -at] = 0, 0
    return out


def _behave(what: str, spec: dict[str, Any]) -> None:
    if what == "error":
        raise RuntimeError(f"cannot read {spec.get('secret', '/data/secret.vsi')}")
    if what == "crash":
        os._exit(70)
    if what == "hang":
        time.sleep(3600)
    if what == "crash-once":
        marker = Path(spec["marker"])
        if marker.exists():
            marker.unlink()
            os._exit(70)


def _forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("SlideTwin asked slideio for a name, raw metadata or an auxiliary image")


@dataclass
class Size:
    width: int
    height: int


@dataclass
class LevelInfo:
    size: Size


@dataclass
class Compression:
    name: str


class Scene:
    name = property(_forbidden)
    get_raw_metadata = get_aux_image = get_aux_image_names = get_aux_image_raster = _forbidden

    def __init__(self, spec: dict[str, Any], index: int, slide: dict[str, Any]) -> None:
        self._spec, self._index, self._slide = spec, index, slide
        self.size = tuple(spec["size"])
        self.num_channels = spec["channels"]
        self.num_z_slices, self.num_t_frames = spec["z"], spec["t"]
        self.resolution = tuple(spec["resolution"])
        self.compression = Compression(spec["compression"])
        self.num_zoom_levels = len(spec["levels"])

    def get_zoom_level_info(self, k: int) -> LevelInfo:
        return LevelInfo(Size(*self._spec["levels"][k]))

    def get_channel_data_type(self, c: int) -> np.dtype[Any]:
        return np.dtype(self._spec["dtype"])

    def read_block_from_level(self, level: int, rect: tuple[int, int, int, int]) -> np.ndarray:
        behaviour = self._slide.get("read", "ok")
        _behave(behaviour, self._slide)
        x, y, w, h = rect
        spec = self._spec
        data = pixels(self._index, level, x, y, w, h, spec["channels"], spec["dtype"])
        if spec["layout"] == "ortho":
            lw, lh = spec["levels"][level]
            data = ortho(pixels(self._index, level, 0, 0, lw, lh, spec["channels"], spec["dtype"]))
            data = data[y : y + h, x : x + w]
        if behaviour == "shape":
            return data[:, :-1]
        if behaviour == "dtype":
            return data.astype(np.float32)
        if behaviour == "gray":  # one channel, though the scene reports three
            return data[:, :, 0]
        return data


class Slide:
    get_scene_by_name = get_aux_image = get_aux_image_names = get_aux_image_raster = _forbidden
    raw_metadata = property(_forbidden)

    def __init__(self, spec: dict[str, Any]) -> None:
        self._scenes = [Scene(s, i, spec) for i, s in enumerate(spec["scenes"])]
        self.num_scenes = len(self._scenes)

    def get_scene(self, index: int) -> Scene:
        return self._scenes[index]


def open_slide(path: str, driver: str) -> Slide:
    spec = json.loads(Path(path + ".fake.json").read_text())
    if driver != spec.get("driver", driver):
        raise RuntimeError("wrong driver")
    _behave(spec.get("open", "ok"), spec)
    return Slide(spec)
