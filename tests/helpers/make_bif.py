"""Small Ventana BIF files laid out like a DP 200 scan (Ventana-1.bif), from synthetic pixels.

Each level-0 tile is one camera frame. Frames are cut from a known stitched image at their
stitched positions; the strip of a frame that its right neighbour covers is painted green, so only
"later frames win" stitches it back. Lower levels are the raw frames downsampled cell by cell. The
expected stitched levels are painted frame by frame (the survey's bifstitch rule: ceil(x / 2^k),
later frames on top), an implementation independent of the reader's. IFD 0 is a label image and
IFD 1 a probability map, which a reader must never touch. The XMP mirrors Ventana-1's: serpentine
tile numbers, rows counted from the bottom, LEFT joints between columns and UP joints between rows.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from slidetwin.core.slide_source import RGB

LOSER = (0, 255, 0)
SOFTWARE = "ScanOutputManager 1.1.0.15854"


@dataclass
class BifFile:
    path: Path
    stitched: list[RGB] = field(default_factory=list)
    raw: list[RGB] = field(default_factory=list)


def box(a: RGB, f: int) -> RGB:
    h, w = a.shape[0] // f, a.shape[1] // f
    b = a[: h * f, : w * f].reshape(h, f, w, f, 3).mean(axis=(1, 3))
    return np.rint(b).astype(np.uint8)


def tile_number(row: int, col: int, rows: int, cols: int) -> int:
    """1-based serpentine number; numbering row q runs from the bottom of the area."""
    q = rows - 1 - row
    k = col if q % 2 == 0 else cols - 1 - col
    return q * cols + k + 1


def joints_xml(rows: int, cols: int, overlaps: list[int], confidence: int = 100) -> str:
    out = []
    for row in range(rows):
        for b in range(cols - 1):
            out.append(
                f'<TileJointInfo FlagJoined="1" Confidence="{confidence}" Direction="LEFT" '
                f'Tile1="{tile_number(row, b, rows, cols)}" '
                f'Tile2="{tile_number(row, b + 1, rows, cols)}" '
                f'OverlapX="{overlaps[b]}" OverlapY="0"/>'
            )
    for col in range(cols):
        for row in range(rows - 1, 0, -1):  # from the bottom row up
            out.append(
                f'<TileJointInfo FlagJoined="1" Confidence="{confidence}" Direction="UP" '
                f'Tile1="{tile_number(row, col, rows, cols)}" '
                f'Tile2="{tile_number(row - 1, col, rows, cols)}" OverlapX="0" OverlapY="0"/>'
            )
    return "".join(out)


def encode_info(
    *,
    rows: int,
    cols: int,
    frame: int,
    overlaps: list[int],
    origin: tuple[int, int] = (0, 0),
    model: str = "VENTANA DP 200",
    scan_res: str = "0.25",
    z_layers: str = "1",
    joints: str | None = None,
) -> str:
    joints = joints_xml(rows, cols, overlaps) if joints is None else joints
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n<EncodeInfo Ver=\"2\">"
        '<SlideInfo Rack="0" Slot="2" BaseName="SECRET-BASENAME" OutputFileName="/raid/SECRET.bif">'
        f'<iScan Mode="brightfield" Magnification="40" ScanRes="{scan_res}" '
        f'ScannerModel="{model}" Z-layers="{z_layers}" Z-spacing="1" UserName="SECRET-USER" '
        'Barcode1D="SECRET-BARCODE" Barcode2D="" ScanWhitePoint="235">'
        '<AOI0 Left="1" Top="2" Right="3" Bottom="4" AOIScanned="1"/></iScan></SlideInfo>'
        '<SlideStitchInfo Left="-1" Top="-1" Right="-1" Bottom="-1">'
        f'<ImageInfo AOIScanned="1" AOIIndex="0" NumRows="{rows}" NumCols="{cols}" '
        f'Width="{frame}" Height="{frame}" Pos-X="0" Pos-Y="0">{joints}</ImageInfo>'
        '<FrameInfo AOIScanned="1" AOIIndex="0"/></SlideStitchInfo>'
        f'<AoiOrigin><AOI0 OriginX="{origin[0]}" OriginY="{origin[1]}"/></AoiOrigin>'
        "</EncodeInfo>\x00"
    )


def write_bif(
    path: Path,
    *,
    rows: int = 3,
    cols: int = 4,
    frame: int = 64,
    overlaps: list[int] | None = None,
    levels: int = 4,
    origin_cells: tuple[int, int] = (0, 0),
    extra_cols: int = 1,
    xmp: str | None = None,
    edit_xmp: Callable[[str], str] | None = None,
    level_description: Callable[[int], str] | None = None,
    imagedepth: int = 1,
) -> BifFile:
    """A DP 200-like BIF. `overlaps[b]` is how far column b + 1 slides over column b (pixels)."""
    overlaps = [0, 5, 3][: cols - 1] if overlaps is None else overlaps
    out = BifFile(path)
    xpos = [c * frame - sum(overlaps[:c]) for c in range(cols)]
    w0, h0 = xpos[-1] + frame, rows * frame
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h0, 0:w0]
    truth = np.stack([xx * 255 // w0, yy * 255 // h0, rng.integers(0, 256, (h0, w0))], axis=2)
    truth = truth.astype(np.uint8)
    c0, r0 = origin_cells
    gw, gh = (c0 + cols + extra_cols) * frame, (r0 + rows) * frame
    raw0 = np.full((gh, gw, 3), 236, np.uint8)  # glass outside the scanned area
    for r in range(rows):
        for c in range(cols):
            f = truth[r * frame : (r + 1) * frame, xpos[c] : xpos[c] + frame].copy()
            if c < cols - 1 and overlaps[c]:
                f[:, frame - overlaps[c] :] = LOSER
            raw0[
                (r0 + r) * frame : (r0 + r + 1) * frame, (c0 + c) * frame : (c0 + c + 1) * frame
            ] = f
    for k in range(levels):
        ds = 2**k
        raw = box(raw0, ds) if k else raw0
        fw = frame // ds
        lw, lh = w0 // ds, h0 // ds
        painted = np.zeros((lh + fw, lw + 2 * fw, 3), np.uint8)
        for r in range(rows):
            for c in range(cols):
                dx, dy = math.ceil(xpos[c] / ds), math.ceil(r * frame / ds)
                cell = raw[(r0 + r) * fw : (r0 + r + 1) * fw, (c0 + c) * fw : (c0 + c + 1) * fw]
                painted[dy : dy + fw, dx : dx + fw] = cell
        out.raw.append(raw)
        out.stitched.append(painted[:lh, :lw].copy())
    if xmp is None:
        xmp = encode_info(
            rows=rows, cols=cols, frame=frame, overlaps=overlaps, origin=(c0 * frame, r0 * frame)
        )
    if edit_xmp is not None:
        xmp = edit_xmp(xmp)
    describe = level_description or (lambda k: f"level={k} mag={40 / 2**k:g} quality=95")
    label_xmp = "<?xml version='1.0' ?><Metadata><iScan Barcode1D=\"SECRET-BARCODE\"/></Metadata>"
    with tifffile.TiffWriter(path, bigtiff=True) as tw:
        label = np.full((90, 40, 3), (20, 30, 200), np.uint8)
        tw.write(
            label,
            description="Label_Image",
            software=SOFTWARE,
            metadata=None,
            extratags=[(700, "B", 0, label_xmp.encode(), True)],
        )
        tw.write(np.zeros((90, 40), np.uint8), description="Probability_Image", metadata=None)
        for k, raw in enumerate(out.raw):
            volume = k == 0 and imagedepth > 1  # focal planes as ImageDepth, as the BIF spec allows
            tw.write(
                np.stack([raw] * imagedepth) if volume else raw,
                tile=(imagedepth, frame, frame) if volume else (frame, frame),
                volumetric=volume,
                photometric="rgb",
                compression="zlib",
                description=describe(k),
                resolution=(4e4 / 2**k, 4e4 / 2**k),
                resolutionunit="CENTIMETER",
                metadata=None,
                extratags=[(700, "B", 0, xmp.encode(), True)] if k == 0 else [],
            )
    return out
