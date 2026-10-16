"""Format router (M8): which format a slide is, from its content, and which reader serves it.

Several readers open some formats but silently serve the wrong image (a preview, a label, an
unstitched tile grid), so the reader is chosen from the file's content and never by trying readers
in turn. Each format has one reader. Formats without a validated open reader are refused, with the
way to convert them. Errors name the slide id and the format, never a file name.
"""

from __future__ import annotations

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
            hint="Check SLIDETWIN_SLIDE_DIR and the path in configs/slides/manifest.yaml.",
        )
    with path.open("rb") as f:
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


def _sniff_tiff(path: Path, name: str) -> FormatMatch:
    import tifffile

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
