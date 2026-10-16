"""An independent reader of MRXS level 0, as an oracle for OpenSlide (ported from the WSI survey,
work/mirax/scripts/mrxs_raw.py).

It parses Slidedat.ini, Index.dat and the camera position buffer, and draws every level-0 image
piece at its stage position with Pillow; where pieces overlap, the image with the higher index
wins. The survey found it identical to OpenSlide on 27 of 30 tissue regions; the others differ in
6-7 pixels along one piece boundary, where two pieces tie.
"""

from __future__ import annotations

import configparser
import io
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


class _Ini(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


class Level0Mosaic:
    def __init__(self, mrxs: Path) -> None:
        folder = mrxs.with_suffix("")
        ini = _Ini(interpolation=None, strict=False)
        ini.read_string((folder / "Slidedat.ini").read_bytes().decode("utf-8-sig"))
        general, hier = ini["GENERAL"], ini["HIERARCHICAL"]
        self._files = [
            folder / ini["DATAFILE"][f"FILE_{i}"] for i in range(int(ini["DATAFILE"]["FILE_COUNT"]))
        ]
        self._index = (folder / hier["INDEXFILE"]).read_bytes()
        assert self._index[:5] == b"01.02"
        offset = 5 + len(general["SLIDE_ID"])
        hier_root, nonhier_root = struct.unpack_from("<ii", self._index, offset)
        nonhier = [
            hier[f"NONHIER_{i}_NAME"]
            for i in range(int(hier["NONHIER_COUNT"]))
            for _ in range(int(hier[f"NONHIER_{i}_COUNT"]))
        ]
        positions = self._positions(nonhier, nonhier_root)
        images_x, divisions = (
            int(general["IMAGENUMBER_X"]),
            int(general.get("CameraImageDivisionsPerSide", "1")),
        )
        level0 = ini[hier["HIER_0_VAL_0_SECTION"]]
        concat = 1 << int(level0.get("IMAGE_CONCAT_FACTOR", "0"))
        tw, th = int(level0["DIGITIZER_WIDTH"]), int(level0["DIGITIZER_HEIGHT"])
        fov_cols = images_x // divisions
        self._pieces: list[tuple[int, int, int, int, tuple[int, ...], int, int]] = []
        for entry in sorted(self._hier_entries(hier_root, 0), key=lambda e: e[0]):
            x, y = entry[0] % images_x, entry[0] // images_x
            if concat >= divisions:  # one image holds several camera fields
                per = concat // divisions
                pw, ph = tw // per, th // per
                for j in range(per):
                    for i in range(per):
                        k = (y // divisions + j) * fov_cols + (x // divisions + i)
                        if k < len(positions) and positions[k][0]:
                            px, py = positions[k][1], positions[k][2]
                            self._pieces.append((px, py, pw, ph, entry, i * pw, j * ph))
            else:  # one camera field split into several images
                k = (y // divisions) * fov_cols + (x // divisions)
                if k < len(positions) and positions[k][0]:
                    px = positions[k][1] + (x % divisions) * tw
                    py = positions[k][2] + (y % divisions) * th
                    self._pieces.append((px, py, tw, th, entry, 0, 0))
        self._cache: dict[tuple[int, ...], np.ndarray] = {}

    def _i32(self, pos: int) -> int:
        value: int = struct.unpack_from("<i", self._index, pos)[0]
        return value

    def _hier_entries(self, root: int, record: int) -> list[tuple[int, ...]]:
        """(image index, offset, length, file number) of a hierarchical record."""
        page = self._i32(self._i32(root + 4 * record) + 4)
        out = []
        while page:
            count, following = self._i32(page), self._i32(page + 4)
            out += [
                struct.unpack_from("<iiii", self._index, page + 8 + 16 * e) for e in range(count)
            ]
            page = following
        return out

    def _blob(self, offset: int, length: int, fileno: int) -> bytes:
        with self._files[fileno].open("rb") as f:
            f.seek(offset)
            return f.read(length)

    def _positions(self, nonhier: list[str], root: int) -> list[tuple[int, int, int]]:
        """(flag, x, y) per camera field, from VIMSLIDE_POSITION_BUFFER or (zlib) the
        StitchingIntensityLayer."""
        for layer, packed in (
            ("VIMSLIDE_POSITION_BUFFER", False),
            ("StitchingIntensityLayer", True),
        ):
            if layer in nonhier:
                page = self._i32(self._i32(root + 4 * nonhier.index(layer)) + 4)
                _, _, offset, length, fileno = struct.unpack_from("<iiiii", self._index, page + 8)
                data = self._blob(offset, length, fileno)
                data = zlib.decompress(data) if packed else data
                return [struct.unpack_from("<Bii", data, 9 * i) for i in range(len(data) // 9)]
        raise ValueError("no camera positions")

    def _tile(self, entry: tuple[int, ...]) -> Any:
        if entry not in self._cache:
            im = Image.open(io.BytesIO(self._blob(*entry[1:])))
            self._cache[entry] = np.asarray(im.convert("RGB"))
        return self._cache[entry]

    def read(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        """RGBA uint8 of level 0; alpha 0 where no field was scanned."""
        out = np.zeros((h, w, 4), np.uint8)
        for px, py, pw, ph, entry, ox, oy in self._pieces:
            x0, y0, x1, y1 = max(x, px), max(y, py), min(x + w, px + pw), min(y + h, py + ph)
            if x0 < x1 and y0 < y1:
                tile = self._tile(entry)
                part = tile[oy + y0 - py : oy + y1 - py, ox + x0 - px : ox + x1 - px]
                out[y0 - y : y1 - y, x0 - x : x1 - x, :3] = part
                out[y0 - y : y1 - y, x0 - x : x1 - x, 3] = 255
        return out
