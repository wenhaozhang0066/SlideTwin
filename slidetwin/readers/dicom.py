"""DICOM WSI (M8): one series of VL Whole Slide Microscopy instances, read with wsidicom.

A slide is a folder that holds the series, or one of the series' level files; the other levels are
then looked for in the same folder. pydicom reads the headers alone (no pixel data) to sort the
files: only the VOLUME instances of one series are opened and hashed. Label, overview and thumbnail
instances, DICOMDIR and other files are never opened.

wsidicom reads each level in level coordinates. Levels are exact 2^k reductions of level 0, by
their pixel spacing. Tiles missing from a sparse series are white.

The same headers hold patient and specimen data. Only codecs, sizes, spacings and the series
identifier are read from them, and none of these reaches an error, a log or the provenance.
"""

from __future__ import annotations

import importlib.metadata
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
import wsidicom  # type: ignore[import-untyped]  # wsidicom ships no type information
from pydicom.filereader import read_partial
from pydicom.tag import BaseTag, Tag
from pydicom.uid import (
    HTJ2K,
    JPEG2000,
    UID,
    ExplicitVRLittleEndian,
    HTJ2KLossless,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
)
from wsidicom.config import Settings  # type: ignore[import-untyped]

from slidetwin.core.errors import SlideMetadataError, SlideTwinError
from slidetwin.core.fileset import check_complete
from slidetwin.core.formats import FormatMatch
from slidetwin.core.geometry import LevelTransform, Point
from slidetwin.core.slide_source import (
    RGB,
    BaseSource,
    ReaderOptions,
    read_error,
    resolve_mpp,
    to_rgb,
    unreadable_error,
)
from slidetwin.readers.tiff import MPP_RANGE

WSI_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.77.1.6"
FILL = (255, 255, 255)
DECODE_THREADS = 4
JPEG_XL = "1.2.840.10008.1.2.4.112"
JPEG_XL_LOSSLESS = "1.2.840.10008.1.2.4.110"

# (transfer syntax -> photometric interpretations) that wsidicom with imagecodecs decodes to the
# right RGB. J2K-family codestreams carry their own colour transform, so RGB, YBR_ICT and YBR_RCT
# decode alike; J2K with YBR_FULL would keep its YCbCr samples and is refused.
_MCT = ("RGB", "YBR_ICT", "YBR_RCT")
SERVED: dict[str, tuple[str, ...]] = {  # checked on real slides (survey: Leica-4, JP2K-33003)
    JPEGBaseline8Bit: ("YBR_FULL_422",),
    JPEG2000: ("YBR_FULL_422",),
}
EXPERIMENTAL: dict[str, tuple[str, ...]] = {  # checked on synthetic series only
    JPEGBaseline8Bit: ("YBR_FULL", "RGB"),
    JPEG2000Lossless: _MCT,
    JPEG2000: _MCT,
    HTJ2KLossless: _MCT,
    HTJ2K: _MCT,
    JPEG_XL_LOSSLESS: ("RGB",),
    JPEG_XL: ("RGB",),
    ExplicitVRLittleEndian: ("RGB",),
}
# DICOM defined terms: any other value from a file is never echoed.
_PHOTOMETRIC_TERMS = frozenset(
    (
        "MONOCHROME1",
        "MONOCHROME2",
        "PALETTE COLOR",
        "RGB",
        "YBR_FULL",
        "YBR_FULL_422",
        "YBR_PARTIAL_422",
        "YBR_PARTIAL_420",
        "YBR_ICT",
        "YBR_RCT",
    )
)
# The header fields read here; all lie before group 0029, so reading stops there (the per-frame
# sequences of a large sparse level come later and take long to parse).
_HEADER_TAGS: list[BaseTag | int] = [
    Tag(keyword)
    for keyword in (
        "SOPClassUID",
        "ImageType",
        "SeriesInstanceUID",
        "SamplesPerPixel",
        "PhotometricInterpretation",
        "Rows",
        "Columns",
        "BitsAllocated",
        "BitsStored",
        "PixelRepresentation",
    )
]


@dataclass(frozen=True, slots=True)
class _Instance:
    """What the header of one VOLUME instance says (codes and numbers only)."""

    file: str
    series: str
    transfer_syntax: str
    photometric: str
    samples: int
    bits: tuple[int, int, int]  # allocated, stored, pixel representation
    tile: tuple[int, int]


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> DicomSource:
    options.refuse_unused(name, match.format)
    return DicomSource(
        path, name=name, mpp_override=mpp_override, experimental=options.experimental
    )


class DicomSource(BaseSource):
    def __init__(
        self,
        path: Path,
        *,
        name: str,
        mpp_override: Point | None = None,
        experimental: bool = False,
    ) -> None:
        root, instances = _series(path, name)
        decoder = _check_codecs(instances, name, experimental)
        # inside the folder once links are followed, before wsidicom opens anything
        check_complete(root, [inst.file for inst in instances], name)
        files = [root / inst.file for inst in instances]
        try:
            wsi = wsidicom.WsiDicom.open(files, settings=Settings(preferred_decoder=decoder))
        except Exception as err:
            raise unreadable_error(name, "wsidicom", err) from None
        try:
            levels = _check_pyramid(wsi, len(files), name)
            spacing = levels[0].pixel_spacing  # mm: width = column spacing, height = row spacing
            raw = (float(spacing.width) * 1000, float(spacing.height) * 1000)
            trusted = raw if _plausible(raw) else (None, None)
            mpp = resolve_mpp(trusted[0], trusted[1], mpp_override, name)
            dims = [(int(lv.size.width), int(lv.size.height)) for lv in levels]
        except SlideTwinError:
            wsi.close()
            raise
        except Exception as err:
            wsi.close()
            raise unreadable_error(name, "wsidicom", err) from None
        self._wsi = wsi
        self._numbers = [int(lv.level) for lv in levels]
        # The files the slide needs: the registry checks and hashes exactly these.
        self.fileset = (root, tuple(sorted(inst.file for inst in instances)))
        version = importlib.metadata.version("wsidicom")
        super().__init__(
            dims,
            mpp,
            f"wsidicom {version}",
            name,
            transforms=[LevelTransform(2.0**k, 2.0**k) for k in self._numbers],
            provenance={
                "format": "dicom",
                "mpp_source": "mpp_override" if mpp_override else "dicom PixelSpacing",
                "fill": list(FILL),
                "decoder": decoder,
                "versions": {
                    "wsidicom": version,
                    "pydicom": pydicom.__version__,
                    "imagecodecs": importlib.metadata.version("imagecodecs"),
                },
            },
        )

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        try:
            with self._lock:
                arr = self._wsi.read_region(
                    (x, y), self._numbers[level], (w, h), threads=DECODE_THREADS, as_array=True
                )
        except Exception as err:
            raise read_error(self._name, level, err) from None
        return to_rgb(np.asarray(arr))

    def close(self) -> None:
        self._wsi.close()


def _series(path: Path, name: str) -> tuple[Path, list[_Instance]]:
    """The folder and the VOLUME instances of the slide's series, sorted by file name."""
    if path.is_dir():
        root, wanted = path, None
    else:
        root = path.parent
        entry = _header(path, name)
        if entry is None:
            raise SlideMetadataError(
                f"Slide '{name}' points to a DICOM file that is not a whole-slide pyramid level.",
                hint="Point the manifest at the series folder or at one of its level files.",
            )
        wanted = entry.series
    found = [
        inst
        for p in sorted(root.iterdir())
        if p.is_file() and (inst := _header(p, name)) is not None
    ]
    series = sorted({inst.series for inst in found})
    if wanted is None and len(series) != 1:
        raise SlideMetadataError(
            f"Slide '{name}' is a folder with {len(series)} DICOM whole-slide series; "
            "SlideTwin reads one series per slide.",
            hint="Put each series in its own folder, or point the manifest at one level file.",
        )
    keep = wanted if wanted is not None else series[0]
    return root, [inst for inst in found if inst.series == keep]


def _header(path: Path, name: str) -> _Instance | None:
    """The instance's header if it is a VOLUME instance of a VL whole-slide image, else None."""
    try:
        with path.open("rb") as f:
            if f.read(132)[128:132] != b"DICM":
                return None  # not DICOM (a checksum stamp, a note)
            f.seek(0)
            ds = read_partial(f, stop_when=_past_header, specific_tags=_HEADER_TAGS)
    except Exception as err:  # unreadable, or a header that ends too early
        raise _damaged(name, err) from None
    try:
        meta = ds.file_meta
        value = ds.get("ImageType", "")
        image_type = [value] if isinstance(value, str) else [str(v) for v in value]
        if (
            str(meta.get("MediaStorageSOPClassUID", "")) != WSI_SOP_CLASS
            or str(ds.get("SOPClassUID", "")) != WSI_SOP_CLASS
            or image_type[2:3] != ["VOLUME"]
        ):
            return None  # DICOMDIR, label, overview, thumbnail, annotations
        return _Instance(
            file=path.name,
            series=str(ds.SeriesInstanceUID),
            transfer_syntax=str(meta.TransferSyntaxUID),
            photometric=str(ds.PhotometricInterpretation),
            samples=int(ds.SamplesPerPixel),
            bits=(int(ds.BitsAllocated), int(ds.BitsStored), int(ds.PixelRepresentation)),
            tile=(int(ds.Columns), int(ds.Rows)),
        )
    except Exception as err:  # a required field is missing or malformed
        raise _damaged(name, err) from None


def _past_header(tag: BaseTag, vr: str | None, length: int) -> bool:
    return int(tag) > 0x0028FFFF


def _damaged(name: str, err: BaseException) -> SlideMetadataError:
    return SlideMetadataError(
        f"Slide '{name}' has a DICOM file whose header cannot be read ({type(err).__name__}).",
        hint="The slide's files may be damaged or incompletely copied.",
    )


def _check_codecs(instances: list[_Instance], name: str, experimental: bool) -> str:
    """Refuse levels outside the checked cases; return the decoder to pin."""
    if not instances:
        raise SlideMetadataError(
            f"Slide '{name}' has no DICOM whole-slide pyramid levels (VOLUME instances).",
            hint="Point the manifest at the folder that holds the series' level files.",
        )
    for inst in instances:
        if inst.samples != 3 or inst.bits != (8, 8, 0):
            raise SlideMetadataError(
                f"Slide '{name}' stores {inst.samples}-sample, {inst.bits[1]}-bit pixels; "
                "SlideTwin reads 8-bit RGB brightfield DICOM so far.",
                hint="Fluorescence and other non-RGB DICOM series are not supported yet.",
            )
        served = inst.photometric in SERVED.get(inst.transfer_syntax, ())
        synthetic = inst.photometric in EXPERIMENTAL.get(inst.transfer_syntax, ())
        if not (served or synthetic):
            raise SlideMetadataError(
                f"Slide '{name}' is stored as {_codec_label(inst)}, which SlideTwin does not read.",
                hint="Convert the series to JPEG or JPEG 2000, or to a pyramidal TIFF.",
            )
        if not served and not experimental:
            raise SlideMetadataError(
                f"Slide '{name}' is stored as {_codec_label(inst)}, which SlideTwin has checked "
                "on synthetic files only.",
                hint="Set experimental: true for this slide in the manifest to serve it anyway.",
            )
    if len({inst.tile for inst in instances}) != 1:
        raise SlideMetadataError(f"Slide '{name}' has levels with different tile sizes.")
    native = {inst.transfer_syntax == ExplicitVRLittleEndian for inst in instances}
    if len(native) != 1:
        raise SlideMetadataError(f"Slide '{name}' mixes uncompressed and compressed levels.")
    return "pydicom" if native == {True} else "imagecodecs"


def _codec_label(inst: _Instance) -> str:
    """Codec and colour names from pydicom's tables; values from the file are never echoed."""
    uid = UID(inst.transfer_syntax)
    codec = uid.name if uid.is_transfer_syntax else "an unknown transfer syntax"
    colours = inst.photometric if inst.photometric in _PHOTOMETRIC_TERMS else "unknown"
    return f"{codec} with {colours} colours"


def _check_pyramid(wsi: Any, n_files: int, name: str) -> list[Any]:
    """One pyramid, one level per file, one focal plane and optical path, exact 2^k levels."""
    if len(wsi.pyramids) != 1:
        raise SlideMetadataError(
            f"Slide '{name}' holds {len(wsi.pyramids)} image pyramids; SlideTwin reads one.",
        )
    levels = list(wsi.levels)
    if len(levels) != n_files:
        raise SlideMetadataError(
            f"Slide '{name}' has {n_files} level files that form {len(levels)} pyramid levels; "
            "levels split over several files are not supported yet.",
        )
    base = levels[0]
    w0, h0 = int(base.size.width), int(base.size.height)
    for lv in levels:
        if len(lv.focal_planes) != 1 or len(lv.optical_paths) != 1:
            raise SlideMetadataError(
                f"Slide '{name}' has several focal planes or optical paths (z-stack or "
                "fluorescence); SlideTwin reads single-plane brightfield DICOM so far.",
            )
        f = 2**lv.level
        ratio_y = float(lv.pixel_spacing.height) / float(base.pixel_spacing.height)
        covers = abs(int(lv.size.width) * f - w0) < f and abs(int(lv.size.height) * f - h0) < f
        if abs(math.log2(ratio_y) - lv.level) > 0.01 or not covers:
            raise SlideMetadataError(
                f"Slide '{name}' has a level that is not a 2^k reduction of the whole slide.",
            )
    return levels


def _plausible(mpp: tuple[float, float]) -> bool:
    lo, hi = MPP_RANGE
    return all(math.isfinite(v) and lo <= v <= hi for v in mpp)
