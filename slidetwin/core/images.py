"""Image encoding, pixel hashes and per-session image stores."""

from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

from slidetwin.core.errors import ImageNotFound
from slidetwin.core.slide_source import RGB

SUBSAMPLING = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}
IMAGE_ID_RE = re.compile(r"^[vt]_\d{4,}$")


def pixel_sha256(arr: RGB) -> str:
    """Hash of the raw RGB pixels (shape-prefixed), independent of image encoding."""
    h, w = arr.shape[:2]
    digest = hashlib.sha256(f"{h}x{w}x3:".encode())
    digest.update(np.ascontiguousarray(arr, dtype=np.uint8).tobytes())
    return digest.hexdigest()


def encode_jpeg(arr: RGB, *, quality: int, subsampling: str) -> bytes:
    """Deterministic baseline JPEG without metadata."""
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8), "RGB").save(
        buf,
        format="JPEG",
        quality=quality,
        subsampling=SUBSAMPLING[subsampling],
        optimize=False,
        progressive=False,
    )
    return buf.getvalue()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ImageStore(Protocol):
    def put(self, image_id: str, data: bytes) -> None: ...

    def get(self, image_id: str) -> bytes: ...


def _check_id(image_id: str) -> None:
    if not IMAGE_ID_RE.match(image_id):
        raise ImageNotFound(f"'{image_id}' is not a view or thumbnail id.")


class MemoryImageStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, image_id: str, data: bytes) -> None:
        _check_id(image_id)
        self._data[image_id] = data

    def get(self, image_id: str) -> bytes:
        _check_id(image_id)
        try:
            return self._data[image_id]
        except KeyError:
            raise ImageNotFound(f"Image '{image_id}' was not served in this session.") from None


class DiskImageStore:
    """Images under `root/<id>.jpg`, so long sessions do not hold views in memory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, image_id: str, data: bytes) -> None:
        _check_id(image_id)
        target = self._root / f"{image_id}.jpg"
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def get(self, image_id: str) -> bytes:
        _check_id(image_id)
        target = self._root / f"{image_id}.jpg"
        try:
            return target.read_bytes()
        except FileNotFoundError:
            raise ImageNotFound(f"Image '{image_id}' was not served in this session.") from None
