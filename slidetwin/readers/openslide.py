"""OpenSlide formats (M8): Hamamatsu NDPI, VMS and VMU, 3DHistech MIRAX (MRXS), Sakura SVSlide.

OpenSlide addresses every level in level-0 coordinates, divided by the level's downsample. Where
that downsample is not an integer, reads are resampled at sub-pixel offsets (survey note 1), so
such a level and the coarser ones are dropped; the others are read in their own pixel grid,
exactly. The renderer reduces a finer level instead, which is slower but exact. Associated images
(label, macro, thumbnail) are never read. Of OpenSlide's properties only the resolution is used:
the others carry labels, barcodes and file names.

OpenSlide's errors latch: after a failed read, every call on that handle fails. The handle is then
closed, and the next read opens a new one.
"""

from __future__ import annotations

import configparser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import openslide  # a missing library is reported with the extra to install
import tifffile

from slidetwin.core.errors import SlideMetadataError, SlideTwinError
from slidetwin.core.fileset import check_complete
from slidetwin.core.formats import CONVERT, FormatMatch
from slidetwin.core.geometry import LevelTransform, Point, Size
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

FILL = (255, 255, 255)  # unscanned areas (transparent in OpenSlide): white, as for brightfield

# NDPI tags (tifffile's names)
NDPI_MAGNIFICATION = 65421  # > 0 on pyramid pages; -1 macro, -2 map
NDPI_Z_OFFSET = 65424  # ZOffsetFromSlideCenter, nm
NDPI_TISSUE_INDEX = 65425
NDPI_FLUORESCENCE = 65434


@dataclass(frozen=True, slots=True)
class _Format:
    label: str
    vendor: str  # what openslide.OpenSlide.detect_format() must say
    mpp_source: str  # the field behind openslide.mpp-x/y (survey section 3.8)
    hint: str  # for a file that OpenSlide does not recognise


FORMATS = {
    "hamamatsu-ndpi": _Format(
        "Hamamatsu NDPI",
        "hamamatsu",
        "ndpi resolution tags",
        "The slide file may be damaged or incompletely downloaded.",
    ),
    "hamamatsu-vms": _Format(
        "Hamamatsu VMS",
        "hamamatsu",
        "vms PhysicalWidth/PhysicalHeight",
        "A VMS or VMU slide is its index file plus the image files it names, in one folder.",
    ),
    "mirax": _Format(
        "3DHistech MRXS",
        "mirax",
        "mirax MICROMETER_PER_PIXEL_X/Y",
        "An MRXS slide is the .mrxs file plus the folder of the same name next to it; copy both.",
    ),
    "sakura-svslide": _Format(
        "Sakura SVSlide",
        "sakura",
        "svslide ResolutionMmPerPix",
        "OpenSlide reads only the older SVSlide schema; other SQLite files are not slides.",
    ),
}


@dataclass(frozen=True, slots=True)
class _Layout:
    """What a format's own files say, checked before OpenSlide opens them."""

    label: str
    files: tuple[str, ...] | None = None  # the file set, relative to the entry file's folder
    experimental: bool = False  # validated on synthetic samples only
    planes: int | None = None  # focal planes (NDPI); the one at offset 0 is served
    provenance: dict[str, Any] = field(default_factory=dict)


def open_reader(
    path: Path,
    match: FormatMatch,
    *,
    name: str,
    mpp_override: Point | None,
    options: ReaderOptions,
) -> OpenSlideFamilySource:
    """Checks, in order: OpenSlide recognises the file as the routed format; the format's own
    files (index, variant, file set); the manifest's choices; the file set is complete. Only then
    is the slide opened."""
    spec = FORMATS.get(match.format)
    if spec is None:
        raise SlideMetadataError(
            f"Slide '{name}': the OpenSlide reader does not handle {match.format}."
        )
    if openslide.OpenSlide.detect_format(str(path)) != spec.vendor:
        raise SlideMetadataError(
            f"Slide '{name}' is not a {spec.label} slide that OpenSlide can read.", hint=spec.hint
        )
    layout = _LAYOUTS[match.format](path, name)
    if layout.experimental and not options.experimental:
        raise SlideMetadataError(
            f"Slide '{name}' is a {layout.label} slide, a format validated on synthetic samples "
            "only.",
            hint="Set experimental: true in the slide's manifest entry to serve it anyway.",
        )
    options.refuse_unused(name, match.format, plane=layout.planes is not None)
    if options.plane not in (None, 0):
        raise SlideMetadataError(
            f"Slide '{name}' ({layout.label}): OpenSlide reads plane 0 only, the focal plane at "
            "offset 0.",
            hint="Remove plane from the slide's manifest entry, or set it to 0.",
        )
    fileset = None
    if layout.files is not None:
        fileset = (path.parent, check_complete(path.parent, layout.files, name))
    return OpenSlideFamilySource(
        path, name=name, fmt=match.format, layout=layout, fileset=fileset, mpp_override=mpp_override
    )


class OpenSlideFamilySource(BaseSource):
    """A slide read through OpenSlide, at the levels whose downsample is an integer."""

    def __init__(
        self,
        path: Path,
        *,
        name: str,
        fmt: str,
        layout: _Layout,
        fileset: tuple[Path, tuple[str, ...]] | None = None,
        mpp_override: Point | None = None,
    ) -> None:
        try:
            slide = openslide.OpenSlide(str(path))
        except Exception as err:
            raise unreadable_error(name, "openslide", err) from None
        try:
            pyramid = (slide.level_dimensions, slide.level_downsamples)
            props = slide.properties
            raw = (
                props.get(openslide.PROPERTY_NAME_MPP_X),
                props.get(openslide.PROPERTY_NAME_MPP_Y),
            )
            if not _plausible(raw):  # a placeholder or a screen default; never guessed
                raw = (None, None)
            mpp = resolve_mpp(raw[0], raw[1], mpp_override, name)
            levels = exact_levels(*pyramid)
        except SlideTwinError:
            slide.close()
            raise
        except Exception as err:
            slide.close()
            raise unreadable_error(name, "openslide", err) from None
        self._path = path
        self._slide: openslide.OpenSlide | None = slide
        self._closed = False
        self._pyramid = pyramid
        self._levels = [(index, k) for index, k, _ in levels]
        self._fileset = fileset
        version = openslide.__library_version__
        source = FORMATS[fmt].mpp_source if mpp_override is None else "mpp_override"
        super().__init__(
            [size for _, _, size in levels],
            mpp,
            f"openslide {version}",
            name,
            transforms=[LevelTransform(float(k), float(k)) for _, k, _ in levels],
            provenance={
                "format": fmt,
                "mpp_source": source,
                "fill": list(FILL),
                "downsamples": [k for _, k, _ in levels],
                **layout.provenance,
                "versions": {"openslide": version, "openslide-python": openslide.__version__},
            },
        )

    @property
    def fileset(self) -> tuple[Path, tuple[str, ...]] | None:
        """The files the slide is made of (MRXS, VMS); the registry checks and hashes them."""
        return self._fileset

    def read_level(self, level: int, x: int, y: int, w: int, h: int) -> RGB:
        self._check(level, x, y, w, h)
        index, k = self._levels[level]
        with self._lock:
            try:
                region = self._handle().read_region((x * k, y * k), index, (w, h))
                arr = np.asarray(region)
            except Exception as err:
                self._discard()
                raise read_error(self._name, level, err) from None
        return to_rgb(arr)  # RGBA: transparent (unscanned) areas become white

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._discard()

    def _handle(self) -> openslide.OpenSlide:
        """The open handle; after a discard, a new one, if the file still has the same pyramid."""
        if self._slide is None:
            if self._closed:
                raise RuntimeError("the slide is closed")
            self._slide = openslide.OpenSlide(str(self._path))
            if (self._slide.level_dimensions, self._slide.level_downsamples) != self._pyramid:
                raise RuntimeError("the slide changed on disk since it was opened")
        return self._slide

    def _discard(self) -> None:
        """Close the handle: after an error, OpenSlide fails every later call on it."""
        slide, self._slide = self._slide, None
        if slide is not None:
            slide.close()


def exact_levels(
    dims: tuple[Size, ...], downsamples: tuple[float, ...]
) -> list[tuple[int, int, Size]]:
    """(OpenSlide level, downsample k, size) of the levels OpenSlide reads without resampling:
    k is an integer and the size is the level-0 size divided by k, rounded down or up. MRXS levels
    are 2^k with sizes rounded down; NDPI and VMS levels divide exactly, including those OpenSlide
    decodes from a stored level at 1/2, 1/4 or 1/8 with libjpeg's DCT scaling. A level decoded so
    from a stored level that is not exact can still look exact (1024 / 255 is not 4, but
    ceil(255 / 2) is 1024 / 8), so the levels after the first inexact one are dropped as well."""
    w0, h0 = dims[0]
    out = []
    for index, ((w, h), ds) in enumerate(zip(dims, downsamples, strict=True)):
        k = round(ds)
        if not (k >= 1 and ds == k and w in (w0 // k, -(-w0 // k)) and h in (h0 // k, -(-h0 // k))):
            break
        out.append((index, k, (w, h)))
    return out


def _plausible(raw: tuple[str | None, str | None]) -> bool:
    try:
        values = [float(v or "nan") for v in raw]
    except ValueError:
        return False
    lo, hi = MPP_RANGE
    return all(lo <= v <= hi for v in values)  # also false for nan and infinity


# --- checks of each format's own files, before OpenSlide opens them ------------------------------


def _ndpi(path: Path, name: str) -> _Layout:
    """Only the validated case: pyramid pages of 8-bit RGB JPEG, one tissue region, and a focal
    plane at offset 0 (the plane OpenSlide reads). Fluorescence, JPEG XR and multi-tissue NDPI
    have no public sample."""
    try:
        with tifffile.TiffFile(path, is_ndpi=True) as tif:
            every: Iterable[Any] = tif.pages
            pages = [p for p in every if (p.tags.valueof(NDPI_MAGNIFICATION) or 0) > 0]
            brightfield = all(
                p.compression == 7
                and p.samplesperpixel == 3
                and p.bitspersample == 8
                and not p.tags.valueof(NDPI_TISSUE_INDEX)
                and not p.tags.valueof(NDPI_FLUORESCENCE)
                for p in pages
            )
            planes = {int(p.tags.valueof(NDPI_Z_OFFSET) or 0) for p in pages}
    except Exception as err:
        raise unreadable_error(name, "tifffile", err) from None
    if not (pages and brightfield and 0 in planes):
        raise SlideMetadataError(
            f"Slide '{name}' is an NDPI variant SlideTwin has not validated; it serves 8-bit RGB "
            "JPEG brightfield NDPI with one tissue region and a focal plane at offset 0.",
            hint=CONVERT,
        )
    n = len(planes)
    return _Layout("Hamamatsu NDPI", planes=n, provenance={"plane": 0, "planes": n})


_VMS = "Virtual Microscope Specimen"
_VMU = "Uncompressed Virtual Microscope Specimen"
_VMS_FILES = ("MapFile", "OptimisationFile", "MacroImage")


def _vms(path: Path, name: str) -> _Layout:
    """The index plus every file it names. The macro (a photo of the whole glass, label included)
    is never decoded, but OpenSlide refuses to open a slide whose named macro is missing, so it is
    part of the set. VMU is validated on a synthetic sample only."""
    ini = _read_ini(path, name)
    uncompressed = not ini.has_section(_VMS)
    try:
        group = ini[_VMU if uncompressed else _VMS]
        layers = int(group.get("NoLayers", "1"))
    except (KeyError, ValueError) as err:
        raise _bad_index(name, err) from None
    if layers != 1:
        raise SlideMetadataError(
            f"Slide '{name}' is a VMS z-stack with {layers} focal layers; SlideTwin serves "
            "single-layer VMS slides only.",
            hint=CONVERT,
        )
    names = [v for k, v in group.items() if k == "ImageFile" or k.startswith("ImageFile(")]
    names += [group[k] for k in _VMS_FILES if k in group]
    _check_names(names, name)
    if uncompressed:
        return _Layout(
            "Hamamatsu VMU",
            files=(path.name, *names),
            experimental=True,
            provenance={"variant": "vmu", "experimental": True},
        )
    return _Layout("Hamamatsu VMS", files=(path.name, *names))


_FLUORESCENCE = "SLIDE_TYPE_FLUORESCENCE"
_TILE_FORMATS = {"JPEG", "PNG", "BMP24"}  # OpenSlide's MRXS decoders; JPEG XR has none


def _mirax(path: Path, name: str) -> _Layout:
    """The .mrxs, its folder's Slidedat.ini, the index file and every data file Slidedat.ini
    names. Served: brightfield with JPEG, PNG or BMP tiles and one focal plane. Fluorescence is
    refused: OpenSlide converts the three channels packed in each JPEG to RGB as if they were
    YCbCr, and that clips them (99% of level-0 pixels on Mirax2-Fluorescence-1)."""
    folder = path.name[: -len(".mrxs")]
    ini = _read_ini(path.parent / folder / "Slidedat.ini", name)
    try:
        hier = ini["HIERARCHICAL"]
        layers = {hier[f"HIER_{i}_NAME"]: i for i in range(int(hier["HIER_COUNT"]))}
        zoom = layers["Slide zoom level"]
        tile_formats = {
            ini[hier[f"HIER_{zoom}_VAL_{j}_SECTION"]]["IMAGE_FORMAT"]
            for j in range(int(hier[f"HIER_{zoom}_COUNT"]))
        }
        focus = layers.get("Microscope focus level")
        planes = 1 if focus is None else int(hier[f"HIER_{focus}_COUNT"])
        kind = ini["GENERAL"].get("SLIDE_TYPE")
        data = ini["DATAFILE"]
        names = [hier["INDEXFILE"], *(data[f"FILE_{i}"] for i in range(int(data["FILE_COUNT"])))]
    except (KeyError, ValueError) as err:
        raise _bad_index(name, err) from None
    if kind == _FLUORESCENCE:
        raise SlideMetadataError(
            f"Slide '{name}' is a 3DHistech fluorescence slide. OpenSlide converts its channels "
            "to RGB and clips them, so SlideTwin does not serve it.",
            hint="Export the channels to OME-TIFF with the vendor's software.",
        )
    if kind != "SLIDE_TYPE_BRIGHTFIELD":
        raise SlideMetadataError(
            f"Slide '{name}' is not marked as a brightfield 3DHistech slide; SlideTwin serves "
            "brightfield MRXS only.",
            hint=CONVERT,
        )
    if not tile_formats <= _TILE_FORMATS:
        raise SlideMetadataError(
            f"Slide '{name}' stores MRXS tiles in a format OpenSlide cannot decode (it reads "
            "JPEG, PNG and BMP).",
            hint=CONVERT,
        )
    if planes != 1:
        raise SlideMetadataError(
            f"Slide '{name}' is an MRXS z-stack with {planes} focal planes; SlideTwin serves "
            "single-plane MRXS slides only.",
            hint=CONVERT,
        )
    _check_names(names, name)
    files = (path.name, f"{folder}/Slidedat.ini", *(f"{folder}/{n}" for n in names))
    return _Layout("3DHistech MRXS", files=files)


def _sakura(path: Path, name: str) -> _Layout:
    """A single SQLite file, validated on a synthetic sample of the older schema only."""
    return _Layout("Sakura SVSlide", experimental=True, provenance={"experimental": True})


_LAYOUTS: dict[str, Callable[[Path, str], _Layout]] = {
    "hamamatsu-ndpi": _ndpi,
    "hamamatsu-vms": _vms,
    "mirax": _mirax,
    "sakura-svslide": _sakura,
}


class _Ini(configparser.ConfigParser):
    """An INI index as GLib's key-file parser (OpenSlide's) reads it: '=' only, '#' comments,
    case-sensitive keys, the last of repeated keys wins."""

    def __init__(self) -> None:
        super().__init__(
            interpolation=None, strict=False, delimiters=("=",), comment_prefixes=("#",)
        )

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _read_ini(path: Path, name: str) -> _Ini:
    """UTF-8, with an optional byte-order mark. Bytes that are not UTF-8 are kept as surrogate
    escapes, so a file name that is not UTF-8 is refused rather than changed. Lines end at '\\n'
    and leading whitespace is ignored, as in GLib; configparser alone would read an indented line
    as the continuation of a value."""
    ini = _Ini()
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape").removeprefix("\ufeff")
        ini.read_string("\n".join(line.lstrip() for line in text.split("\n")))
    except (OSError, configparser.Error) as err:
        raise _bad_index(name, err) from None
    return ini


def _check_names(names: Iterable[str], name: str) -> None:
    """Index files name plain files in the slide's own folder. A name with a path separator or an
    unprintable or non-UTF-8 character is refused, and never echoed."""
    for n in names:
        if n in ("", ".", "..") or "/" in n or "\\" in n or not n.isprintable():
            raise SlideMetadataError(
                f"Slide '{name}' has an index that names a file SlideTwin will not open (outside "
                "the slide's folder, or not a valid UTF-8 name).",
                hint="Rename the slide's files and their entries in the index.",
            )


def _bad_index(name: str, err: BaseException) -> SlideMetadataError:
    return SlideMetadataError(
        f"Slide '{name}' has an index file that cannot be read ({type(err).__name__}).",
        hint="The slide's files may be damaged or incompletely copied.",
    )
