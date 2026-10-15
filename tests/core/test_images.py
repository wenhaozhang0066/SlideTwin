from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from slidetwin.core.errors import ImageNotFound
from slidetwin.core.images import (
    SUBSAMPLING,
    DiskImageStore,
    MemoryImageStore,
    encode_jpeg,
    pixel_sha256,
    sha256_bytes,
)


def test_pixel_hash_depends_on_shape_and_content() -> None:
    a = np.zeros((2, 6, 3), np.uint8)
    b = np.zeros((6, 2, 3), np.uint8)
    assert pixel_sha256(a) != pixel_sha256(b)
    c = a.copy()
    c[0, 0, 0] = 1
    assert pixel_sha256(a) != pixel_sha256(c)
    assert pixel_sha256(a) == pixel_sha256(a.copy())


@pytest.mark.parametrize("subsampling", sorted(SUBSAMPLING))
def test_jpeg_is_deterministic(subsampling: str) -> None:
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)
    one = encode_jpeg(arr, quality=90, subsampling=subsampling)
    two = encode_jpeg(arr, quality=90, subsampling=subsampling)
    assert one == two
    assert sha256_bytes(one) == sha256_bytes(two)
    img = Image.open(io.BytesIO(one))
    assert img.size == (64, 48)
    assert img.format == "JPEG"
    assert "exif" not in img.info


def test_memory_store() -> None:
    store = MemoryImageStore()
    store.put("v_0001", b"abc")
    assert store.get("v_0001") == b"abc"
    with pytest.raises(ImageNotFound):
        store.get("v_0002")
    with pytest.raises(ImageNotFound):
        store.put("../etc/passwd", b"x")


def test_disk_store(tmp_path: Path) -> None:
    store = DiskImageStore(tmp_path / "imgs")
    store.put("t_0001", b"jpeg")
    assert (tmp_path / "imgs" / "t_0001.jpg").read_bytes() == b"jpeg"
    assert store.get("t_0001") == b"jpeg"
    with pytest.raises(ImageNotFound):
        store.get("v_0009")
    with pytest.raises(ImageNotFound):
        store.get("../../secret")
