"""Image encoding, pixel hashes and per-session image stores."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

from slidetwin.core.errors import ImageNotFound
from slidetwin.core.slide_source import RGB

SUBSAMPLING = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}
IMAGE_ID_RE = re.compile(r"[vt]_[0-9]{4,9}")


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


def is_image_id(value: object) -> bool:
    return isinstance(value, str) and IMAGE_ID_RE.fullmatch(value) is not None


class ImageStore(Protocol):
    def put(self, image_id: str, data: bytes) -> None: ...

    def get(self, image_id: str) -> bytes: ...

    def discard(self) -> None:
        """Delete every stored image (the session is gone)."""
        ...


def _check_id(image_id: object) -> str:
    if not is_image_id(image_id):
        raise ImageNotFound(f"{str(image_id)[:40]!r} is not a view or thumbnail id.")
    assert isinstance(image_id, str)
    return image_id


class MemoryImageStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, image_id: str, data: bytes) -> None:
        self._data[_check_id(image_id)] = data

    def get(self, image_id: str) -> bytes:
        try:
            return self._data[_check_id(image_id)]
        except KeyError:
            raise ImageNotFound(f"Image '{image_id}' was not served in this session.") from None

    def discard(self) -> None:
        self._data.clear()


class DiskImageStore:
    """Images under `root/<id>.jpg`, so long sessions do not hold views in memory.

    The folder is created on the first image, so a session that never starts leaves nothing.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, image_id: str, data: bytes) -> None:
        target = self._root / f"{_check_id(image_id)}.jpg"
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def get(self, image_id: str) -> bytes:
        target = self._root / f"{_check_id(image_id)}.jpg"
        try:
            return target.read_bytes()
        except FileNotFoundError:
            raise ImageNotFound(f"Image '{image_id}' was not served in this session.") from None

    def discard(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)
