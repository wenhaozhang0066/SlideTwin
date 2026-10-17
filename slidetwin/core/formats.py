"""Format router (M8): which format a slide is, from its content, and which reader serves it.

Several readers open some formats but silently serve the wrong image (a preview, a label, an
unstitched tile grid), so the reader is chosen from the file's content and never by trying readers
in turn. Each format has one reader. Formats without a validated open reader are refused, with the
way to convert them. Errors name the slide id and the format, never a file name.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from slidetwin.core.errors import SlideMetadataError, SlideNotFound

# Formats with no open reader SlideTwin can rely on, or no sample to validate one on.
CONVERT = "Convert it to pyramidal TIFF, OME-TIFF or DICOM with the vendor's software first."
NO_READER = {
    "sqray-sdpc": "SQRAY SDPC has no open reader. " + CONVERT,
    "unic-tmap": "UNIC TMAP has no correct open reader. " + CONVERT,
    "kfbio-kfb": "KFBio KFB has no validated open reader. " + CONVERT,
    "motic-mds": "Motic MDS/MDSX has no open reader. " + CONVERT,
    "dyj": "DYJ has no open reader. " + CONVERT,
    "motic-tiff": "Motic TIFF has not been validated on a real file. " + CONVERT,
    "mikroscan-tiff": "Mikroscan TIFF has not been validated on a real file. " + CONVERT,
    "plain-image": "Plain images (JPEG, PNG, WebP, BMP, JPEG 2000) are not supported yet. "
    "Save the image as a pyramidal TIFF with its resolution.",
}

_TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PLAIN = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",
    b"BM",
    b"\x00\x00\x00\x0cjP  \r\n\x87\n",  # JPEG 2000 (JP2)
    b"\xff\x4f\xff\x51",  # JPEG 2000 codestream
)
_VENDOR_MAGIC = {
    b"SQ1.": "sqray-sdpc",
    b"TMAP": "unic-tmap",
    b"BKIO": "motic-mds",
    b"DPTWSI": "dyj",
}


# Every format name the router gives, with or without a reader. A reader plugin may not give one of
# them to a file the router does not know (slidetwin/readers/plugins.py).
BUILT_IN_FORMATS = frozenset(
    {
        "akoya-qptiff",
        "aperio-svs",
        "argos-avs",
        "dicom",
        "dyj",
        "generic-tiff",
        "hamamatsu-ndpi",
        "hamamatsu-vms",
        "huron-tiff",
        "kfbio-kfb",
        "leica-scn",
        "mikroscan-tiff",
        "mirax",
        "motic-mds",
        "motic-tiff",
        "ole2",
        "olympus-vsi",
        "ome-tiff",
        "ome-zarr",
        "philips-isyntax",
        "philips-tiff",
        "plain-image",
        "sakura-svslide",
        "sqray-sdpc",
        "trestle-tiff",
        "unic-tmap",
        "unknown",
        "ventana-bif",
        "zeiss-czi",
    }
)


@dataclass(frozen=True, slots=True)
class FormatMatch:
    """`reader` is None when the format is refused; `reason` then says what to do instead."""

    format: str
    reader: str | None
    reason: str = ""


def sniff(path: Path, name: str = "slide") -> FormatMatch:
    """The slide's format from its content (and, for a few multi-file formats, its extension).
    `name` is the public slide id, the only identifier used in errors."""
    if path.is_dir():
        return _sniff_dir(path)
    if not path.is_file():
        raise SlideNotFound(
            f"The file for slide '{name}' was not found.",
            hint="Check the slide folder (SLIDETWIN_SLIDE_DIR) and the slide's manifest path.",
        )
    with path.open("rb") as f:  # an OSError (unreadable) is reported by the registry
        head = f.read(512)
    if head[128:132] == b"DICM":  # also dual-personality DICOM/TIFF files
        return FormatMatch("dicom", "wsidicom")
    if head.startswith(b"ZISRAWFILE"):
        return FormatMatch("zeiss-czi", "czi")
    if head.startswith(_OLE2):
        return FormatMatch("ole2", "zvi")  # Zeiss ZVI; Motic MDS is refused by that reader
    if head.startswith(b"SQLite format 3\x00"):
        return FormatMatch("sakura-svslide", "openslide")
    suffix = path.suffix.lower()
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if suffix == ".mrxs":
        return FormatMatch("mirax", "openslide")
    if text.startswith((b"[Virtual Microscope Specimen]", b"[Uncompressed Virtual Microscope")):
        return FormatMatch("hamamatsu-vms", "openslide")
    if b"DPUfsImport" in head[:256] and not head.startswith(_TIFF_MAGIC):
        return FormatMatch("philips-isyntax", "isyntax")
    if suffix == ".vsi":
        return FormatMatch("olympus-vsi", "slideio")
    if head.startswith(_TIFF_MAGIC):
        return _sniff_tiff(path, name)
    if head.startswith(_PLAIN) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return FormatMatch("plain-image", None, NO_READER["plain-image"])
    for magic, fmt in _VENDOR_MAGIC.items():
        if head.startswith(magic):
            return FormatMatch(fmt, None, NO_READER[fmt])
    return FormatMatch("unknown", None, "SlideTwin does not recognise this file. " + CONVERT)


def _sniff_dir(path: Path) -> FormatMatch:
    if (path / "zarr.json").is_file() or (path / ".zattrs").is_file():
        return FormatMatch("ome-zarr", "zarr")
    if any(_is_dicom(p) for p in sorted(path.iterdir())[:64] if p.is_file()):
        return FormatMatch("dicom", "wsidicom")
    return FormatMatch("unknown", None, "This folder holds no DICOM series or OME-Zarr image.")


def _is_dicom(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(132)[128:132] == b"DICM"


# Limits checked before tifffile parses a TIFF: it loads every page's tile offsets into memory,
# about 20 bytes per declared tile, so a crafted header could ask for gigabytes.
MAX_TILES = 1 << 22  # tiles or strips per page: 4 million, a 500k x 500k px level of 256 px tiles
MAX_PIXELS = 1 << 40  # pixels per page: about 10^12
MAX_PAGES = 4096
_TILE_TAGS = {273, 279, 324, 325}  # StripOffsets, StripByteCounts, TileOffsets, TileByteCounts


def check_tiff_size(path: Path, name: str) -> None:
    """Refuse a TIFF whose pages declare implausibly many tiles or pixels, before any reader
    loads them. Only top-level pages that parse as classic or BigTIFF IFDs are judged; where the
    structure is unusual (NDPI's offsets above 4 GB, damage), tifffile decides."""
    with path.open("rb") as f:
        head = f.read(16)
        o = "<" if head[:2] == b"II" else ">"
        big = struct.unpack(o + "H", head[2:4])[0] == 43
        # BigTIFF: 8-byte entry counts, counts, values and offsets; classic TIFF: 2, 4, 4, 4.
        n_fmt, word = ("Q", "Q") if big else ("H", "I")
        n_size, w = struct.calcsize(n_fmt), struct.calcsize(word)
        entry = 4 + 2 * w
        offset = struct.unpack(o + word, head[8:16] if big else head[4:8])[0]
        seen: set[int] = set()
        while offset and offset not in seen and len(seen) < MAX_PAGES:
            seen.add(offset)
            f.seek(offset)
            raw = f.read(n_size)
            n = struct.unpack(o + n_fmt, raw)[0] if len(raw) == n_size else 0
            data = f.read(n * entry) if 0 < n <= 4096 else b""
            if not data or len(data) < n * entry:
                return
            if _too_large(data, n, entry, o, word):
                raise SlideMetadataError(
                    f"Slide '{name}' is a TIFF that declares implausibly many tiles or pixels.",
                    hint="The file may be damaged; SlideTwin does not open it.",
                )
            raw = f.read(w)
            offset = struct.unpack(o + word, raw)[0] if len(raw) == w else 0


def _too_large(data: bytes, n: int, entry: int, o: str, word: str) -> bool:
    """Whether one IFD's tile or strip arrays, or its pixel count, exceed the limits."""
    w = struct.calcsize(word)
    counts: dict[int, int] = {}
    dims: dict[int, int] = {}
    for at in range(0, n * entry, entry):
        tag, kind = struct.unpack(o + "HH", data[at : at + 4])
        number = struct.unpack(o + word, data[at + 4 : at + 4 + w])[0]
        if tag in _TILE_TAGS:
            counts[tag] = number
        elif tag in (256, 257) and number == 1 and kind in (3, 4):  # SHORT or LONG
            fmt = "H" if kind == 3 else "I"
            size = struct.calcsize(fmt)
            dims[tag] = struct.unpack(o + fmt, data[at + 4 + w : at + 4 + w + size])[0]
    pixels = dims.get(256, 0) * dims.get(257, 0)
    return max(counts.values(), default=0) > MAX_TILES or pixels > MAX_PIXELS


def _sniff_tiff(path: Path, name: str) -> FormatMatch:
    import tifffile

    check_tiff_size(path, name)
    try:
        with tifffile.TiffFile(path) as tif:
            page = tif.pages.first
            flags = {
                name
                for name in (
                    "ndpi",
                    "scn",
                    "bif",
                    "philips",
                    "svs",
                    "ome",
                    "qpi",
                    "trestle",
                    "mikroscan",
                    "motic",
                )
                if getattr(tif, f"is_{name}", False)
            }
            make = str(page.tags.valueof("Make") or "")
            argos = str(page.tags.valueof(65000) or "").startswith("<Argos")
    except Exception as err:  # a damaged TIFF: report it like any unreadable slide
        raise SlideMetadataError(
            f"Slide '{name}' is not a readable TIFF ({type(err).__name__}).",
            hint="The slide file may be damaged or incompletely downloaded.",
        ) from None
    for flag, match in (
        ("ndpi", FormatMatch("hamamatsu-ndpi", "openslide")),
        ("scn", FormatMatch("leica-scn", "scn")),
        ("bif", FormatMatch("ventana-bif", "bif")),
        ("philips", FormatMatch("philips-tiff", "tifffile")),
        ("trestle", FormatMatch("trestle-tiff", "trestle")),
        ("mikroscan", FormatMatch("mikroscan-tiff", None, NO_READER["mikroscan-tiff"])),
        ("motic", FormatMatch("motic-tiff", None, NO_READER["motic-tiff"])),
        ("svs", FormatMatch("aperio-svs", "tifffile")),
        ("ome", FormatMatch("ome-tiff", "ome")),
        ("qpi", FormatMatch("akoya-qptiff", "qptiff")),
    ):
        if flag in flags:
            return match
    if make.strip().lower().startswith("huron"):
        return FormatMatch("huron-tiff", "tifffile")
    if argos:
        return FormatMatch("argos-avs", "argos")
    return FormatMatch("generic-tiff", "tifffile")
