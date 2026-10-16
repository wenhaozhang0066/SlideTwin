"""Synthetic slides in OpenSlide's formats, written by the tests; nothing binary is committed.

Ported from the WSI survey's generators (work/ndpi/scripts/make_synth_ndpi_v2.py,
work/vms/scripts/build_vmu.py, work/cn_vendors/scripts/make_synthetic_svslide.py), with synthetic
pixels instead of crops of CMU-1:
- NDPI: the byte layout measured on real files. Classic TIFF magic, 8-byte IFD offsets and a block
  of 4-byte high words after each IFD; one JPEG strip (YCbCr 4:4:4) per page, with restart markers
  and McuStarts on level 0; the NDPI tags 65420-65428; a macro page.
- VMS: an index, a grid of JPEGs with restart markers, a map at 1/8 scale, an optimisation file and
  a canary macro (no label). VMU: the uncompressed variant, NGR images of 12-bit samples.
- SVSlide: Sakura's older SQLite schema, one grayscale JPEG per tile and channel.
"""

from __future__ import annotations

import io
import sqlite3
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from slidetwin.core.slide_source import RGB

SHORT, LONG, RATIONAL, SLONG, FLOAT, ASCII = 3, 4, 5, 9, 11, 2
_PACK = {SHORT: "H", LONG: "I", SLONG: "i", FLOAT: "f"}
Tag = tuple[int, int, Any]  # code, TIFF type, value


def jpeg(img: RGB, *, restart_blocks: int | None = None, quality: int = 90) -> bytes:
    """Baseline JPEG, YCbCr 4:4:4 (as NDPI and VMS store it); restart markers every
    `restart_blocks` MCUs of 8x8 px."""
    buf = io.BytesIO()
    kw: dict[str, Any] = {"quality": quality, "subsampling": 0}
    if restart_blocks:
        kw["restart_marker_blocks"] = restart_blocks
    Image.fromarray(img).save(buf, "JPEG", **kw)
    return buf.getvalue()


def _mcu_starts(data: bytes) -> list[int]:
    """Offsets of the entropy-coded segments: after the SOS header and after every RST marker."""
    pos = 2
    while data[pos + 1] != 0xDA:
        pos += 2 + struct.unpack(">H", data[pos + 2 : pos + 4])[0]
    starts = [pos + 2 + struct.unpack(">H", data[pos + 2 : pos + 4])[0]]
    i = starts[0]
    while (j := data.find(b"\xff", i)) >= 0 and j + 1 < len(data):
        if 0xD0 <= data[j + 1] <= 0xD7:
            starts.append(j + 2)
        i = j + 2
    return starts


def plane_image(width: int, height: int, plane: int) -> RGB:
    """Distinct content per focal plane: red, green or blue, a grid every 64 px and a diagonal."""
    yy, xx = np.mgrid[0:height, 0:width]
    base = np.array([[200, 60, 60], [60, 200, 60], [60, 60, 200]][plane % 3], np.float32)
    img = base * (0.6 + 0.4 * (xx / width))[..., None]
    img[(xx % 64 < 3) | (yy % 64 < 3)] = 255
    img[np.abs(xx * height / width - yy) < 3] = 0
    return np.clip(img, 0, 255).astype(np.uint8)


class _NdpiWriter:
    """TIFF pages in NDPI's layout: data first, then each IFD followed by its high words."""

    def __init__(self) -> None:
        self.buf = bytearray(b"II" + struct.pack("<H", 42) + struct.pack("<Q", 0))
        self.pages: list[list[Tag]] = []

    def add_data(self, data: bytes) -> int:
        if len(self.buf) % 2:
            self.buf += b"\0"
        self.buf += data
        return len(self.buf) - len(data)

    def finish(self) -> bytes:
        next_ptr = 4  # where the header points to the first IFD
        for tags in self.pages:
            entries = []
            for code, typ, value in sorted(tags, key=lambda t: t[0]):
                values = value if isinstance(value, list) else [value]
                if typ == ASCII:
                    raw = str(value).encode("latin-1") + b"\0"
                elif typ == RATIONAL:
                    raw = b"".join(struct.pack("<II", n, d) for n, d in values)
                else:
                    raw = b"".join(struct.pack("<" + _PACK[typ], v) for v in values)
                count = len(raw) if typ == ASCII else len(values)
                if len(raw) <= 4:
                    entries.append((code, typ, count, raw.ljust(4, b"\0")))
                else:
                    entries.append((code, typ, count, struct.pack("<I", self.add_data(raw))))
            if len(self.buf) % 2:
                self.buf += b"\0"
            self.buf[next_ptr : next_ptr + 8] = struct.pack("<Q", len(self.buf))
            self.buf += struct.pack("<H", len(entries))
            for code, typ, count, value_field in entries:
                self.buf += struct.pack("<HHI", code, typ, count) + value_field
            next_ptr = len(self.buf)
            self.buf += struct.pack("<Q", 0) + b"\0\0\0\0" * len(entries)  # next IFD, high words
        return bytes(self.buf)


def write_ndpi(
    path: Path,
    *,
    size: tuple[int, int] = (1024, 768),
    level1: tuple[int, int] = (256, 192),
    planes: Sequence[int] = (0,),
    px_per_cm: tuple[int, int] | None = (21910, 21975),
    mcu_starts: bool = True,
    page_tags: Sequence[Tag] = (),
) -> dict[tuple[int, int], bytes]:
    """A two-level NDPI: level 0 with restart markers, level 1 a single strip (as in 2009 files),
    one page per focal plane z (nm) and level, then a macro. `page_tags` are added to (or replace)
    the tags of every pyramid page. Returns the JPEG strip of each (level, z) page."""
    w0, h0 = size
    common: list[Tag] = [
        (258, SHORT, [8, 8, 8]),
        (259, SHORT, 7),
        (262, SHORT, 6),
        (271, ASCII, "Hamamatsu"),
        (272, ASCII, "NanoZoomer"),
        (277, SHORT, 3),
        (296, SHORT, 3),
        (530, SHORT, [1, 1]),
        (65420, SHORT, 1),
        (65422, SLONG, 0),
        (65423, SLONG, 0),
    ]
    writer = _NdpiWriter()
    strips: dict[tuple[int, int], bytes] = {}
    for level, (lw, lh) in enumerate((size, level1)):
        f = w0 / lw
        for i, z in enumerate(planes):
            img = plane_image(w0, h0, i)
            if level:
                img = np.asarray(Image.fromarray(img).resize((lw, lh), Image.Resampling.BOX))
            data = jpeg(img, restart_blocks=32 if level == 0 else None)
            strips[(level, z)] = data
            offset = writer.add_data(data)
            tags = [
                *common,
                (256, LONG, lw),
                (257, LONG, lh),
                (273, LONG, offset),
                (278, LONG, lh),
                (279, LONG, len(data)),
                (65421, FLOAT, 20.0 / f),
                (65424, SLONG, z),
                (65425, LONG, 0),
                (65428, LONG, 0),
            ]
            if px_per_cm is not None:
                tags += [
                    (282, RATIONAL, [(round(px_per_cm[0] / f), 1)]),
                    (283, RATIONAL, [(round(px_per_cm[1] / f), 1)]),
                ]
            if level == 0 and mcu_starts:
                tags.append((65426, LONG, _mcu_starts(data)))
            replaced = {code for code, _, _ in page_tags}
            writer.pages.append([t for t in tags if t[0] not in replaced] + list(page_tags))
    macro = jpeg(np.full((100, 300, 3), 128, np.uint8))
    offset = writer.add_data(macro)
    writer.pages.append(
        [
            *common,
            (256, LONG, 300),
            (257, LONG, 100),
            (273, LONG, offset),
            (278, LONG, 100),
            (279, LONG, len(macro)),
            (65421, FLOAT, -1.0),
        ]
    )
    path.write_bytes(writer.finish())
    return strips


def canary_macro(size: tuple[int, int] = (400, 137)) -> bytes:
    """A stand-in for the photo of the glass slide; a test fails if these pixels are ever read."""
    img = np.zeros((size[1], size[0], 3), np.uint8)
    img[..., 1] = 255
    img[::40, :] = (255, 0, 255)
    return jpeg(img)


def vms_image(width: int, height: int, seed: int = 0) -> RGB:
    """Level-0 pixels: smooth colour ramps with noise and dark lines every 128 px."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    img = np.stack([200 - xx * 100 // width, 120 + yy * 100 // height, 150 + 0 * xx], -1)
    img = img + rng.integers(-12, 13, img.shape)
    img[(xx % 128 == 5) | (yy % 128 == 9)] = 40
    return np.clip(img, 0, 255).astype(np.uint8)


def _mean8(img: RGB) -> RGB:
    h, w = img.shape[0] // 8, img.shape[1] // 8
    block = img[: h * 8, : w * 8].reshape(h, 8, w, 8, 3).astype(np.float64).mean(axis=(1, 3))
    return np.rint(block).astype(np.uint8)


def write_vms(
    folder: Path,
    *,
    cols: Sequence[int] = (1024,),
    rows: Sequence[int] = (512,),
    mpp: tuple[float, float] = (0.25, 0.25),
    optimisation: bool = True,
    macro: bool = True,
    extra: Sequence[str] = (),
) -> Path:
    """A VMS slide in `folder`: the index `s.vms`, JPEG cells `s.jpg` and `s(c,r).jpg` (restart
    markers every 256 px; cell widths must be multiples of 256), the map `s_map2.jpg` (8x8 means),
    a junk optimisation file `s.opt` (OpenSlide ignores a damaged one) and a canary macro
    `s_macro.jpg`. `extra` lines are added to the index. Returns the index path."""
    folder.mkdir(parents=True, exist_ok=True)
    width, height = sum(cols), sum(rows)
    img = vms_image(width, height)
    lines = [
        "[Virtual Microscope Specimen]",
        "NoLayers=1",
        f"NoJpegColumns={len(cols)}",
        f"NoJpegRows={len(rows)}",
    ]
    y0 = 0
    for r, rh in enumerate(rows):
        x0 = 0
        for c, cw in enumerate(cols):
            cell = "s.jpg" if (c, r) == (0, 0) else f"s({c},{r}).jpg"
            (folder / cell).write_bytes(jpeg(img[y0 : y0 + rh, x0 : x0 + cw], restart_blocks=32))
            lines.append(("ImageFile" if (c, r) == (0, 0) else f"ImageFile({c},{r})") + f"={cell}")
            x0 += cw
        y0 += rh
    (folder / "s_map2.jpg").write_bytes(jpeg(_mean8(img), restart_blocks=width // 64))
    lines.append("MapFile=s_map2.jpg")
    if optimisation:
        (folder / "s.opt").write_bytes(b"\x01junk" * 8)
        lines.append("OptimisationFile=s.opt")
    lines += [
        "SourceLens=40.000000",
        f"PhysicalWidth={round(mpp[0] * 1000 * width)}",
        f"PhysicalHeight={round(mpp[1] * 1000 * height)}",
    ]
    if macro:
        (folder / "s_macro.jpg").write_bytes(canary_macro())
        lines.append("MacroImage=s_macro.jpg")
    lines += [*extra, "Reference=synthetic"]
    index = folder / "s.vms"
    index.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return index


def _write_ngr(path: Path, rgb8: RGB, column_width: int) -> None:
    """NGR as OpenSlide reads it: 'GN', width, height and column width (int32 at 4, 8, 12), data
    offset (int32 at 24); then column strips of 3 x uint16 per pixel, here 12-bit (v << 4 | 9)."""
    h, w, _ = rgb8.shape
    values = (rgb8.astype(np.uint16) << 4) | 0x9
    header = bytearray(32)
    header[0:2] = b"GN"
    struct.pack_into("<iii", header, 4, w, h, column_width)
    struct.pack_into("<i", header, 24, len(header))
    strips = [values[:, c : c + column_width] for c in range(0, w, column_width)]
    path.write_bytes(bytes(header) + b"".join(s.astype("<u2").tobytes() for s in strips))


def write_vmu(
    folder: Path,
    *,
    size: tuple[int, int] = (512, 256),
    column_width: int = 128,
    mpp: tuple[float, float] = (0.25, 0.25),
) -> tuple[Path, RGB, RGB]:
    """A VMU slide in `folder`: the index `u.vmu`, the NGR image `u.ngr`, the NGR map `u_map.ngr`
    (8x8 means) and a canary macro. Returns the index and the 8-bit level 0 and map."""
    folder.mkdir(parents=True, exist_ok=True)
    level0 = vms_image(*size, seed=1)
    small = _mean8(level0)
    _write_ngr(folder / "u.ngr", level0, column_width)
    map_cw = column_width // 8 if small.shape[1] % (column_width // 8) == 0 else small.shape[1]
    _write_ngr(folder / "u_map.ngr", small, map_cw)
    (folder / "u_macro.jpg").write_bytes(canary_macro())
    lines = [
        "[Uncompressed Virtual Microscope Specimen]",
        "NoLayers=1",
        "ImageFile=u.ngr",
        "MapFile=u_map.ngr",
        "BitsPerPixel=36",
        "PixelOrder=RGB",
        "SourceLens=40.000000",
        f"PhysicalWidth={round(mpp[0] * 1000 * size[0])}",
        f"PhysicalHeight={round(mpp[1] * 1000 * size[1])}",
        "MacroImage=u_macro.jpg",
    ]
    index = folder / "u.vmu"
    index.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return index, level0, small


def write_svslide(
    path: Path, img: RGB, *, tile: int = 256, mm_per_px: float = 0.000499
) -> list[RGB]:
    """A Sakura SVSlide (older schema, as documented by OpenSlide): tiles `T;x|y;ds;channel;0`
    in BlobStore, one grayscale JPEG per channel, levels halved until one tile holds the image.
    Returns the levels as written (before JPEG)."""
    h, w = img.shape[:2]
    db = sqlite3.connect(path)
    c = db.cursor()
    c.execute("CREATE TABLE DataManagerSQLiteConfigXPO (OID integer primary key, TableName text)")
    c.execute("INSERT INTO DataManagerSQLiteConfigXPO (TableName) VALUES ('BlobStore')")
    c.execute("CREATE TABLE BlobStore (id text primary key, size integer, data blob)")
    c.execute(
        "CREATE TABLE SVSlideDataXPO (OID integer primary key, m_labelScan integer,"
        " m_overviewScan integer, SlideId text, Date text, Description text, Creator text,"
        " DiagnosisCode text, HRScanCount integer, Keywords text, TotalDataSizeBytes integer)"
    )
    c.execute(
        "CREATE TABLE SVHRScanDataXPO (OID integer primary key, ParentSlide integer, ScanId text,"
        " Date text, Description text, Name text, PosOnSlideMm blob, ResolutionMmPerPix real,"
        " NominalLensMagnification real, ThumbnailImage blob, TotalDataSizeBytes integer,"
        " FocussingMethod integer, FocusStack blob)"
    )
    c.execute(
        "CREATE TABLE SVScannedImageDataXPO (OID integer primary key, Id text, PosOnSlideMm blob,"
        " ScanCenterPosMm blob, ResolutionMmPerPix real, Image blob, ThumbnailImage blob)"
    )
    blobs: dict[str, Any] = {"++MagicBytes": "SVGigaPixelImage", "++VersionBytes": "1.0.0"}
    blobs["Header"] = struct.pack("<IIIIIII", tile, w, h, 8, 1, 3, 1) + struct.pack(
        "<H6I", 256, 1, 2, 3, 4, 5, 6
    )
    levels = [img]
    ds = 1
    while True:
        level = levels[-1]
        lh, lw = level.shape[:2]
        for ty in range(0, lh, tile):
            for tx in range(0, lw, tile):
                cell = np.full((tile, tile, 3), 255, np.uint8)
                part = level[ty : ty + tile, tx : tx + tile]
                cell[: part.shape[0], : part.shape[1]] = part
                for ch in range(3):
                    gray = Image.fromarray(np.ascontiguousarray(cell[:, :, ch]))
                    buf = io.BytesIO()
                    gray.save(buf, "JPEG", quality=95)
                    blobs[f"T;{tx * ds}|{ty * ds};{ds};{ch};0"] = buf.getvalue()
        if max(lh, lw) <= tile:
            break
        ds *= 2
        small = Image.fromarray(img).resize((w // ds, h // ds), Image.Resampling.BOX)
        levels.append(np.asarray(small))
    total = sum(len(v) for v in blobs.values() if isinstance(v, bytes))
    for key, value in blobs.items():
        c.execute(
            "INSERT INTO BlobStore (id, size, data) VALUES (?, ?, ?)", (key, len(value), value)
        )
    c.execute("INSERT INTO BlobStore (id, size, data) VALUES ('TOTAL_SIZE', ?, x'')", (total,))
    label, thumb = canary_macro((128, 64)), jpeg(levels[-1])
    scanned = "INSERT INTO SVScannedImageDataXPO (OID, Id, ResolutionMmPerPix, Image) VALUES "
    c.execute(scanned + "(?, ?, ?, ?)", (1, "label", 0.01, label))
    c.execute(scanned + "(?, ?, ?, ?)", (2, "overview", 0.02, thumb))
    c.execute(
        "INSERT INTO SVSlideDataXPO VALUES (1, 1, 2, 'slide', '2026-10-16', 'synthetic', 'test',"
        " '0', 1, '', ?)",
        (total,),
    )
    c.execute(
        "INSERT INTO SVHRScanDataXPO VALUES (1, 1, 'scan', '2026-10-16', 'synthetic', 'scan', ?,"
        " ?, 20.0, ?, ?, 1, ?)",
        (b"\0" * 16, mm_per_px, thumb, total, b"\0" * 8),
    )
    db.commit()
    db.close()
    return levels
