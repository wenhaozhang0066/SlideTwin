"""Synthetic DICOM WSI series for the reader tests (M8).

Each pyramid level is one VL Whole Slide Microscopy instance with its tiles as frames, written with
pydicom and encoded with imagecodecs. A series can also hold label, overview and thumbnail
instances (solid magenta, so a test sees at once if one is ever served). The patient fields hold
made-up values, so a test can check that nothing from a header reaches errors or the provenance.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import imagecodecs
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence as DicomSequence
from pydicom.uid import (
    HTJ2K,
    JPEG2000,
    UID,
    ExplicitVRLittleEndian,
    HTJ2KLossless,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGLSLossless,
    RLELossless,
    generate_uid,
)

WSI_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.77.1.6"
JPEG_XL = UID("1.2.840.10008.1.2.4.112")
JPEG_XL_LOSSLESS = UID("1.2.840.10008.1.2.4.110")
JPEG_XL_RECOMPRESSION = UID("1.2.840.10008.1.2.4.111")

# Made-up identity: a test asserts that none of it reaches an error, a log or the provenance.
PATIENT_NAME = "Synthetic^Patient^Leak"
PATIENT_ID = "SYNTH-PATIENT-0001"
BARCODE = "SYNTH-BARCODE-77"
MAGENTA = (255, 0, 255)


def _jpeg(subsampling: str, rgb: bool = False) -> Callable[[np.ndarray], bytes]:
    def encode(tile: np.ndarray) -> bytes:
        if rgb:
            return bytes(
                imagecodecs.jpeg8_encode(tile, level=95, colorspace="RGB", outcolorspace="RGB")
            )
        return bytes(imagecodecs.jpeg8_encode(tile, level=95, subsampling=subsampling))

    return encode


def _j2k(*, reversible: bool, mct: bool) -> Callable[[np.ndarray], bytes]:
    def encode(tile: np.ndarray) -> bytes:
        level = 0 if reversible else 60
        out = imagecodecs.jpeg2k_encode(
            tile, level=level, codecformat="J2K", reversible=reversible, mct=mct
        )
        return bytes(out)

    return encode


def _htj2k(*, reversible: bool) -> Callable[[np.ndarray], bytes]:
    def encode(tile: np.ndarray) -> bytes:
        # level is OpenJPH's quantisation step for lossy coding; None is reversible
        return bytes(imagecodecs.htj2k_encode(tile, level=None if reversible else 0.01))

    return encode


def _jpegxl(*, lossless: bool) -> Callable[[np.ndarray], bytes]:
    def encode(tile: np.ndarray) -> bytes:
        if lossless:
            return bytes(imagecodecs.jpegxl_encode(tile, lossless=True))
        return bytes(imagecodecs.jpegxl_encode(tile, level=95))

    return encode


def _jpegxl_recompressed(tile: np.ndarray) -> bytes:
    jpeg = imagecodecs.jpeg8_encode(tile, level=95, subsampling="444")
    return bytes(imagecodecs.jpegxl_encode_jpeg(jpeg))


@dataclass(frozen=True)
class Codec:
    """How a level's frames are stored. `encode` is None for native (uncompressed) frames."""

    transfer_syntax: UID
    photometric: str
    encode: Callable[[np.ndarray], bytes] | None
    lossy: bool


CODECS: dict[str, Codec] = {
    "jpeg": Codec(JPEGBaseline8Bit, "YBR_FULL_422", _jpeg("422"), True),
    "jpeg-444": Codec(JPEGBaseline8Bit, "YBR_FULL", _jpeg("444"), True),
    "jpeg-rgb": Codec(JPEGBaseline8Bit, "RGB", _jpeg("444", rgb=True), True),
    "j2k-ict": Codec(JPEG2000, "YBR_ICT", _j2k(reversible=False, mct=True), True),
    "j2k-rgb": Codec(JPEG2000, "RGB", _j2k(reversible=False, mct=False), True),
    "j2k-lossless-rct": Codec(JPEG2000Lossless, "YBR_RCT", _j2k(reversible=True, mct=True), False),
    "j2k-lossless-rgb": Codec(JPEG2000Lossless, "RGB", _j2k(reversible=True, mct=False), False),
    "htj2k": Codec(HTJ2K, "RGB", _htj2k(reversible=False), True),
    "htj2k-lossless": Codec(HTJ2KLossless, "RGB", _htj2k(reversible=True), False),
    "jpegxl": Codec(JPEG_XL, "RGB", _jpegxl(lossless=False), True),
    "jpegxl-lossless": Codec(JPEG_XL_LOSSLESS, "RGB", _jpegxl(lossless=True), False),
    "native": Codec(ExplicitVRLittleEndian, "RGB", None, False),
    # Not served: wsidicom cannot read JPEG XL recompressed JPEG; J2K with YBR_FULL would be
    # decoded without its colour transform; JPEG-LS and RLE have not been checked. Where no
    # encoder is given the frames stay raw: the reader refuses on the header first.
    "jpegxl-jpeg": Codec(JPEG_XL_RECOMPRESSION, "YBR_FULL", _jpegxl_recompressed, True),
    "j2k-ybr-full": Codec(JPEG2000, "YBR_FULL", _j2k(reversible=False, mct=False), True),
    "jpegls": Codec(JPEGLSLossless, "RGB", None, False),
    "rle": Codec(RLELossless, "RGB", None, False),
}


@dataclass
class Series:
    """The files of one synthetic series."""

    levels: list[Path]
    others: list[Path] = field(default_factory=list)
    series_uid: str = ""


def _code(value: str, scheme: str, meaning: str) -> Dataset:
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def _tiles(image: np.ndarray, tile: int) -> list[tuple[int, int, np.ndarray]]:
    """(row, column, full-size tile) in row-major order; edge tiles are padded with black, which a
    correct reader never shows."""
    h, w = image.shape[:2]
    out = []
    for r in range(-(-h // tile)):
        for c in range(-(-w // tile)):
            block = np.zeros((tile, tile, 3), np.uint8)
            part = image[r * tile : (r + 1) * tile, c * tile : (c + 1) * tile]
            block[: part.shape[0], : part.shape[1]] = part
            out.append((r, c, block))
    return out


def _dataset(
    image: np.ndarray,
    *,
    spacing_mm: tuple[float, float],
    tile: int,
    codec: Codec,
    uids: dict[str, str],
    image_type: str,
    sparse: bool,
    missing: Collection[tuple[int, int]],
    instance: int,
) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = WSI_SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = codec.transfer_syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = WSI_SOP_CLASS
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = uids["study"]
    ds.SeriesInstanceUID = uids["series"]
    ds.FrameOfReferenceUID = uids["frame"]
    ds.Modality = "SM"
    ds.InstanceNumber = instance
    ds.PatientName = PATIENT_NAME
    ds.PatientID = PATIENT_ID
    ds.ContainerIdentifier = BARCODE
    ds.BarcodeValue = BARCODE
    first = "DERIVED" if image_type == "VOLUME" and instance > 1 else "ORIGINAL"
    ds.ImageType = [first, "PRIMARY", image_type, "NONE" if instance == 1 else "RESAMPLED"]
    h, w = image.shape[:2]
    tiles = _tiles(image, tile) if image_type == "VOLUME" else [(0, 0, image)]
    rows, cols = (tile, tile) if image_type == "VOLUME" else (h, w)
    kept = [(r, c, t) for r, c, t in tiles if (r, c) not in missing]
    if sparse:
        kept = sorted(kept, key=lambda rct: (-rct[0], rct[1]))  # bottom tile row first, as Leica
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = codec.photometric
    ds.PlanarConfiguration = 0
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = len(kept)
    ds.TotalPixelMatrixColumns, ds.TotalPixelMatrixRows = w, h
    ds.TotalPixelMatrixFocalPlanes = 1
    ds.ImagedVolumeWidth = w * spacing_mm[0]
    ds.ImagedVolumeHeight = h * spacing_mm[1]
    ds.ImagedVolumeDepth = 0.0
    ds.DimensionOrganizationType = "TILED_SPARSE" if sparse else "TILED_FULL"
    ds.ImageOrientationSlide = [0, -1, 0, -1, 0, 0]
    origin = Dataset()
    origin.XOffsetInSlideCoordinateSystem = 20.0
    origin.YOffsetInSlideCoordinateSystem = 40.0
    ds.TotalPixelMatrixOriginSequence = DicomSequence([origin])
    path = Dataset()
    path.OpticalPathIdentifier = "1"
    path.IlluminationTypeCodeSequence = DicomSequence(
        [_code("111744", "DCM", "Brightfield illumination")]
    )
    path.IlluminationColorCodeSequence = DicomSequence([_code("414298005", "SCT", "Full Spectrum")])
    ds.OpticalPathSequence = DicomSequence([path])
    ds.NumberOfOpticalPaths = 1
    measures = Dataset()
    measures.PixelSpacing = [f"{spacing_mm[1]:.12g}", f"{spacing_mm[0]:.12g}"]  # row, column
    measures.SliceThickness = "0"
    shared = Dataset()
    shared.PixelMeasuresSequence = DicomSequence([measures])
    ds.SharedFunctionalGroupsSequence = DicomSequence([shared])
    if sparse:
        per_frame = []
        for r, c, _ in kept:
            pos = Dataset()
            pos.ColumnPositionInTotalImagePixelMatrix = c * tile + 1
            pos.RowPositionInTotalImagePixelMatrix = r * tile + 1
            pos.XOffsetInSlideCoordinateSystem = 20.0
            pos.YOffsetInSlideCoordinateSystem = 40.0
            pos.ZOffsetInSlideCoordinateSystem = 0.0
            frame = Dataset()
            frame.PlanePositionSlideSequence = DicomSequence([pos])
            per_frame.append(frame)
        ds.PerFrameFunctionalGroupsSequence = DicomSequence(per_frame)
    raw = [np.ascontiguousarray(t).tobytes() for _, _, t in kept]
    if not codec.transfer_syntax.is_encapsulated:
        ds.PixelData = b"".join(raw)
    else:
        frames = [codec.encode(t) for _, _, t in kept] if codec.encode else raw
        ds.PixelData = encapsulate(frames, has_bot=True)
        ds["PixelData"].VR = "OB"
        ds.LossyImageCompression = "01" if codec.lossy else "00"
    return ds


def write_series(
    folder: Path,
    levels: Sequence[np.ndarray],
    *,
    mpp: tuple[float, float] = (0.5, 0.5),
    tile: int = 64,
    codec: str = "jpeg",
    sparse: bool = False,
    missing: Collection[tuple[int, int, int]] = (),
    others: Sequence[str] = ("LABEL", "OVERVIEW"),
    series_uid: str | None = None,
    names: Sequence[str] | None = None,
) -> Series:
    """Write one instance per level (level k has pixel spacing mpp * 2^k) and the associated
    instances named in `others`. `missing` holds (level, tile row, tile column) that are never
    stored; it needs `sparse`."""
    folder.mkdir(parents=True, exist_ok=True)
    uids = {
        "study": generate_uid(),
        "series": series_uid or generate_uid(),
        "frame": generate_uid(),
    }
    spec = CODECS[codec]
    out = Series([], [], uids["series"])
    for k, image in enumerate(levels):
        spacing = (mpp[0] * 2**k / 1000, mpp[1] * 2**k / 1000)
        gaps = {(r, c) for lv, r, c in missing if lv == k}
        ds = _dataset(
            image,
            spacing_mm=spacing,
            tile=tile,
            codec=spec,
            uids=uids,
            image_type="VOLUME",
            sparse=sparse,
            missing=gaps,
            instance=k + 1,
        )
        path = folder / (names[k] if names else f"level{k}.dcm")
        ds.save_as(path, enforce_file_format=True)
        out.levels.append(path)
    plain = CODECS["native"]
    for i, kind in enumerate(others):
        image = np.full((48, 80, 3), MAGENTA, np.uint8)
        ds = _dataset(
            image,
            spacing_mm=(0.05, 0.05),
            tile=0,
            codec=plain,
            uids=uids,
            image_type=kind,
            sparse=False,
            missing=(),
            instance=100 + i,
        )
        path = folder / f"{kind.lower()}.dcm"
        ds.save_as(path, enforce_file_format=True)
        out.others.append(path)
    return out


def read_header(path: Path) -> Dataset:
    return pydicom.dcmread(path, stop_before_pixels=True)
