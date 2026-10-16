# SlideTwin: plan for M8 (every WSI format)

Status: approved by the owner (2026-10-16) with the recommended decisions, except decision 2
(OpenSlide in the public image), which waits for the choice of the final form (an installable
program or a hosted website). Formats without an open reader are refused; the README lists what is
supported.
Requirement (owner, 2026-10-15): SlideTwin must support all whole-slide image formats.

Evidence: a survey of 29 scanner formats, 7 open formats and 8 plain image types. Each format was
probed with the readers that apply to it (OpenSlide 4.0.1, tiffslide 4.0, tifffile, wsidicom,
slideio 2.10, pylibCZIrw, pyisyntax, Bio-Formats 8.5), on real samples where they exist. Formats
without a sample or an open reader were researched only. The results are kept outside the
repository. A second reviewer checked this plan against them.

## What changes

Today SlideTwin reads single-file slides with tiffslide (default) or OpenSlide. The survey found:

- Several readers open some formats but silently serve the wrong image: a preview, a label, an
  unstitched tile grid, or wrong colours. "The first reader that opens it" is unsafe.
- Some slides are several files or a folder: MRXS, VMS, VMU, VSI, DICOM, NDPIS, AFI, OME-Zarr.
- Some slides hold several scenes, regions of interest (ROIs), focal planes or fluorescence channels.
- Some formats have no open reader at all.
- The five CAMELYON16 demo slides are Philips TIFF. tifffile reads them identically to OpenSlide
  (mean absolute difference 0), about 3.5 times faster, so they stay on the base install.

## Principles

- **One validated reader per format.** The router picks it from the file's content. There is no
  silent fallback: if that reader is not installed, the slide is refused with the extra to install.
  The manifest may name another reader only if it is validated for the sniffed format.
- **Refuse outside the tested case.** Each reader checks the variants the survey validated (for
  example channels, bit depth, joint direction, number of areas) and refuses others.
- **Replay stays exact.** The trajectory header records the reader's provenance; replay refuses a
  slide whose provenance differs from the log.

## Architecture

- `slidetwin/core/slide_source.py` keeps the `SlideSource` protocol. New members have defaults, so
  existing sources keep working:
  - `level_transform(level)`: scale and offset from level pixels to level-0 pixels. The default is
    D13's extent ratio. Trestle's coarse levels need an offset.
  - `fill_rgb`: the colour of unscanned areas inside the slide (white for brightfield, black for
    fluorescence). The profile's fill still paints the area outside the slide.
  - `scanned_mask(level, x, y, w, h)`: optional; where tiles were never scanned. Coverage does not
    count those cells as tissue.
  - `provenance`: reader and decoder versions, format, mpp source field, and indices and numbers for
    scene, plane, channels, windows, fill and converter. No names from the file.
- `slidetwin/core/formats.py`: content sniffing gives a `FormatMatch` (format, reader, entry file,
  file set). The manifest's `backend` keeps working; the new default is `auto`.
- `slidetwin/core/fileset.py`: the files a slide needs, their completeness check and their hash.
- `slidetwin/core/channels.py`: deterministic rendering of non-RGB data.
- `slidetwin/readers/`: one module per reader family, importing its library lazily.
- `trajectory.py` and `replay.py`: an optional `provenance` block in the header (schema 1.0 stays
  valid); replay compares it with the opened source.

## Manifest (backward compatible)

New optional fields: `format`, `scene`, `plane`, `channels`, `fileset_sha256`, `archive` and
`experimental`. `sha256` keeps its meaning (the entry file), so existing entries, including `.mrxs`
or `.vms` entries read with `backend: openslide`, are unchanged.

```yaml
- id: mrxs_demo
  path: mirax/CMU-1.mrxs                 # entry file; its data folder is part of the file set
  fileset_sha256: <hash of the file set>
  archive: {url: https://..., sha256: <zip hash>, member: CMU-1-Saved-1_16/}
- id: dicom_demo
  path: dicom/series-1.2.3/              # a folder: one series; only VOLUME instances are used
- id: czi_demo
  path: zeiss/slide.czi
  scene: 1                                # one SlideTwin slide per scene or ROI
- id: argos_demo
  path: argos/slide.avs
  plane: 2                                # an index; multi-plane slides must say which
- id: fluo_demo
  path: ome/slide.ome.tiff
  channels: {show: [0, 3, 5]}             # indices; windows come from the file or are computed once
```

## Routing

Content signatures, checked in this order (survey section 3.1). A format claimed by extension
(`.mrxs`, `.vsi`, `.vms`, `.ndpis`, `.afi`) is refused when its files are incomplete; it never falls
through to another reader (a lone `.mrxs` is a JPEG preview).

| Signature | Format | Reader |
|---|---|---|
| `DICM` at byte 128 + VL Whole Slide SOP class | DICOM WSI (also dual TIFF/DICOM) | wsidicom |
| `ZISRAW` | Zeiss CZI | czifile |
| OLE2 `D0CF11E0` | Zeiss ZVI (slideio); Motic MDS (refused) | per stream |
| `.mrxs` that is a JPEG + folder with `Slidedat.ini` | 3DHistech MIRAX | OpenSlide `detect_format() == 'mirax'` |
| `.vsi` + `_<name>_/stack*/frame_t.ets` | Olympus VSI | slideio |
| INI `[Virtual Microscope Specimen]` / `[Uncompressed Virtual Microscope Specimen]` | Hamamatsu VMS / VMU | OpenSlide |
| `[NanoZoomer Digital Pathology Image Set]`; `<ImageList>` | NDPIS; Aperio AFI | own parsers |
| XML with `DPUfsImport` in the first 256 bytes | Philips iSyntax v1 (v2, interface 100.x, refused) | pyisyntax |
| TIFF + NDPI tags | Hamamatsu NDPI | OpenSlide |
| TIFF + vendor marker: Aperio; Make `Huron`; tag 65000 `<Argos`; `Philips DP v1.0` + `DPUfsImport`; `MedScan` + Trestle; iScan XMP; SCN XML; OME-XML; `PerkinElmer-QPI` | per vendor | table below |
| SQLite | Sakura SVSlide (OpenSlide); iBL (refused) | |
| `SQ1.`, `TMAP0x`, `BKIO`, `DPTWSI` and other vendor magics | no open reader | refused |
| `zarr.json` / `.zattrs` with `multiscales`, or a bioformats2raw `OME/` root | OME-Zarr | zarr |
| JP2 box or J2K codestream; JPEG, PNG, WebP, BMP | plain images | below |
| other tiled TIFF | generic pyramidal TIFF | tiffslide, after the structure check |

- Structure check for generic TIFF: IFD 0 is tiled and the largest; every other level is a reduced
  IFD or SubIFD with the same samples and type, strictly decreasing, with the same aspect ratio
  within floor rounding; nothing precedes or interleaves the pyramid. Otherwise refused. Motic,
  Mikroscan and OptraScan TIFF are refused until a sample is tested.
- Planar RGB stored as three channels (PhenoCycler H&E) is detected and served as RGB, not as
  fluorescence.
- The original file extension is kept: tifffile and NDPI depend on it.

## Formats and readers

"Synthetic" marks formats validated only on samples made in the survey. Those are served only when
the manifest entry sets `experimental: true`.

| Format | Reader | Custom code and limits |
|---|---|---|
| Aperio SVS (JPEG, JPEG 2000 33003/33005); SVS exports (Grundium, KFBio) | tiffslide | missing tiles white |
| Generic tiled TIFF, libvips pyramids | tiffslide | structure check |
| Philips TIFF, sparse (the demo slides) | tifffile `series[0]`, fill 255 | OpenSlide by override |
| Huron TIFF | tiffslide | none |
| ARGOS AVS (z-stack) | tifffile 'Baseline' series, fill 255 | plane order unverified on real files |
| Leica SCN, 1 ROI, brightfield | tifffile | SCN XML parser; macro never read |
| Leica SCN multi-ROI, mixed resolution, fluorescence (synthetic) | tifffile | ROI = scene; 3 channels, 8 bit only |
| Trestle TIFF | tifffile | stitcher; coarse-level offset; verified on CMU-1 only |
| Ventana BIF, DP 200 | tifffile | stitcher from XMP joints; 1 area, LEFT/UP joints, Z = 1 only; older iScan BIF refused |
| Hamamatsu NDPI | OpenSlide | z = 0; tifffile by override |
| Hamamatsu VMS; VMU (synthetic) | OpenSlide | none |
| 3DHistech MRXS, brightfield and fluorescence | OpenSlide | fluorescence channel remap; JPEG XR tiles refused |
| Sakura SVSlide, old schema (synthetic) | OpenSlide | new schema refused |
| DICOM WSI: JPEG, JPEG 2000, sparse | wsidicom | VOLUME instances of one series only |
| DICOM WSI: JPEG XL, HTJ2K (synthetic) | wsidicom | none |
| Zeiss CZI, brightfield, single file (JPEG XR, zstd, uncompressed; scenes) | czifile | compositor: tile order, BGR, shift by bit count − 8 |
| Zeiss CZI fluorescence | pylibCZIrw (LGPL-3.0, extra) | 16-bit zstd1-HiLo refused (garbled) |
| Zeiss ZVI | slideio | int16 read as uint16; 14-bit windows; ortho exports refused; scene names never used |
| Olympus VSI | slideio | 3-channel uint8 only; no lookup by scene name |
| Philips iSyntax v1 | pyisyntax | level offset fix; refused when mpp is unknown |
| OME-TIFF RGB (QuPath, raw2ometiff, vips) | tiffslide | none |
| OME-TIFF multichannel, Z, planar | tifffile | OME-XML parser |
| OME-TIFF written by bfconvert from TIFF/JPEG (planar, big-endian) | none correct | refused with the conversion route |
| Akoya QPTIFF brightfield; fluorescence | tiffslide; tifffile | pages grouped by image type |
| NDPIS, Aperio AFI (synthetic) channel sets | NDPI / SVS readers | index parsers |
| OME-Zarr NGFF 0.4 / 0.5 | zarr | NGFF parser |
| inForm component TIFF (float32, one field) | none | refused: not a WSI |
| Plain images | see below | |
| SDPC, TMAP, KFB/KFBF, Motic MDS/MDSX, TRON, ZYP, iBL, DYJ, iSyntax v2, MRXS JPEG XR, Omnyx, Aperio CWS, D-Sight, SWS, WebView, DZI/ZIF | no open reader or no sample | refused with the conversion route |

## Files and integrity

- A reader lists the files a slide needs: for example `.mrxs` + `Slidedat.ini` + `Index.dat` + every
  `DataNNNN.dat`; for DICOM the VOLUME instances of one series (found with pydicom by SOP class,
  ImageType and SeriesInstanceUID; label and overview files are never opened or hashed); for
  Trestle only the `.tif`. The list is checked for completeness and exact names before opening.
- `fileset_sha256`: sha256 over `slidetwin-fileset-v1\n` followed by one JSON line
  `[relative path, size, sha256]` per file, sorted by path bytes. A single file keeps its plain
  sha256 (D9 unchanged). The trajectory header records both.
- `fetch_slides` learns zip archives: the archive's sha256 is checked first, members are extracted
  safely (no absolute paths or `..`), then the file set is checked.

## Geometry, fill and coverage

- Each source gives its own level transform (a change to D13; see the defaults below).
- OpenSlide resamples levels whose downsample is not an integer. It is used only for formats whose
  downsamples are integers; existing `backend: openslide` entries keep today's behaviour.
- Brightfield fill is white. A fill colour from the file is used for fluorescence only; OpenSlide's
  `background-color` is ignored (Trestle's E6E6E6 is wrong).

## Non-RGB data

- Brightfield data with more than 8 bits is shifted by (bit count − 8), from the file's bit count.
  Signed or float data is refused unless the manifest sets a window.
- Fluorescence: colours from the file, then emission wavelength, then channel name, then a fixed
  palette. Windows from the file, then exact quantiles (0.1%, 99.9%) of a fixed reference level of
  at least 1 megapixel, computed once and cached. Integer look-up tables, additive blend.
- The settings go into the provenance, so a replay renders the same pixels.

## Safety

- Pyramid pages are chosen by structure, not by the reader's guess. Labels, macros, previews,
  overview instances and other ROIs are never opened.
- Only sizes and mpp are read from metadata. Raw properties, XML, series and scene names, barcodes
  and serial numbers never reach a response or a log. Names shown to readers come from the manifest,
  where `_check_no_leak` checks them. Errors name the slide id and the exception type only.
- mpp: each format's trusted field (survey section 3.8), then a plausibility check (0.05-20 um/px,
  not a dpi default, not unit NONE). Otherwise the slide needs `mpp_override`.

## Robustness

- OpenSlide: downsamples are cached at open; after a read error the handle is reopened.
- pyisyntax: one thread for the whole process (calls from other threads crash it).
- slideio (VSI, ZVI only) runs in a worker process, so a crash fails one read, not the server.
- tifffile and tiffslide decode with at most 4 threads.

## Plain images

- JPEG 2000 serves its own resolution levels (glymur). BMP is memory-mapped.
- Single-level tiled TIFF and BigTIFF get derived levels, built tile by tile. Stripped TIFF works
  with small strips (tiffslide) or uncompressed data (memory map); a single huge compressed strip is
  refused with the conversion route.
- JPEG, PNG and WebP must be decoded whole: 3.8-5.8 GB of memory per gigapixel. They are converted
  only up to a cap (default 1 gigapixel; the 2-core Codespace has 8 GB).
- Conversion runs in the background; the slide is listed as preparing until it is done. The cached
  lossless pyramidal TIFF (`cache/`) is keyed by the source hash and converter version and verified
  by its own stored hash, since it holds served pixels.

## Packaging and licences

- Base install (permissive): tiffslide, tifffile, imagecodecs, wsidicom, pydicom, czifile, zarr.
- Extras: `openslide` (LGPL-2.1, `openslide-bin>=4.0.1.1`), `slideio` (BSD; add the notices of the
  libraries it bundles), `isyntax` (MIT; add libisyntax's BSD-2 text), `glymur` (MIT; needs
  libopenjp2), `czi-fluorescence` (pylibCZIrw, LGPL-3.0).
- Not shipped: Bio-Formats (GPL) and vendor SDKs. A reader plugin hook (`slidetwin.readers` entry
  point) lets a user install an adapter locally.
- The public Docker image gets the base and the permissive extras. OpenSlide goes in only if the
  owner agrees (decision 2), with its licence and its source archive in the image. The Codespace is
  not distribution and installs every extra.

## Tests

- Fixtures are generated in the tests or downloaded with a pinned sha256 into git-ignored
  `data/testdata/`; nothing is committed.
- Tier 1, every CI run (about 97 MB): the survey's list, including the CC BY 4.0 NDPIS bands and the
  AFI set built from them.
- Tier 2, opt-in download (at most 100 MB each): Huron-1, iSyntax (CC BY 4.0), Leica-4 DICOM,
  Zeiss-5-JXR, Leica-Fluorescence-1, JP2K-33003 (DICOM and SVS), Zeiss-1 ZVI.
- Tier 3, nightly or opt-in (mostly 167-310 MB each): CMU-1.ndpi, Hamamatsu-2.ndpi (the privacy
  test), CMU-1.tiff, Philips-4, Ventana-1, Trestle, OS-2 VSI, a 34 MB range-read replica of
  Hamamatsu-1 (over 4 GB).
- Exact pixel hashes are pinned only for the decoder that SlideTwin ships. Comparisons with
  independent readers use tolerances (for example mean difference at most 0.1, maximum 2).
- Coverage: `slidetwin/core` keeps its 100% gate. A second job installs all extras, OpenSlide
  included, and gates `slidetwin/readers` at 100%, with synthetic fixtures or a mocked library
  where no CI sample exists (VSI, BIF, Philips sparse, Huron, iSyntax, ZVI, QPTIFF, DICOM J2K).

## Order of work

1. Core: protocol members, provenance in the header and in replay, file sets and hashing, manifest
   fields, the router and its refusals, archive fetching. The rest builds on it.
2. Readers, in parallel: TIFF family (SVS, generic, Philips, Huron, ARGOS, OME-TIFF, QPTIFF);
   stitchers (SCN, Trestle, BIF); OpenSlide formats; DICOM; CZI, ZVI and VSI; iSyntax; the channel
   renderer with NDPIS, AFI and OME-Zarr; plain images.
3. Packaging, extras, the Docker image and the Codespace.
4. Conformance and safety tests, an adversarial review, and the docs: a format support table in the
   README, DECISIONS and ARCHITECTURE.

## Decisions for the owner

| # | Question | Recommended | Alternative and consequence |
|---|---|---|---|
| 1 | Formats with no open reader (SDPC, TMAP, KFB, Motic, TRON, iSyntax v2) | refuse with the conversion route; plugin hook for local adapters | own readers: TMAP and KFB have MIT readers (TMAP's is wrong below level 0, KFB's untested); SDPC needs a legal review (HEVC); TRON needs vendor keys |
| 2 | OpenSlide (LGPL) in the public Docker image | yes, with its licence and source archive | no: NDPI, VMS, MRXS and Sakura only in the Codespace and local installs |
| 3 | Bio-Formats (GPL) | not shipped; own BIF stitcher (DP 200, one area) | none needed: the plugin hook covers a local install |
| 4 | Embedded colour profiles (ICC) | serve raw device RGB as today; record whether a profile exists | apply profiles: colours change by 6-54 grey levels on average |
| 5 | Fluorescence display | fixed per slide; agents cannot change channels | add channel actions to the session API |
| 6 | Plain images | background conversion to a lossless pyramid; JPEG/PNG/WebP up to 1 gigapixel (lossless: about 28 s and 2.6 GB per 50k x 50k image) | refuse non-pyramidal images |
| 7 | Python 3.11 (the SPEC says 3.11+) | require 3.12 (czifile and tiffslide 4 need it) | keep 3.11 without CZI and with tiffslide 3 |
| 8 | Test data | generated or downloaded with pinned hashes; non-CC0 samples only as opt-in downloads | also download non-CC0 samples in CI |

Technical defaults that apply unless the owner objects:

- Level transforms per source instead of D13's single rule.
- One SlideTwin slide per scene or ROI; no composite canvas.
- Multi-plane slides must name a plane, except NDPI (z = 0) and ARGOS (middle plane, unverified).
- mpp plausible between 0.05 and 20 um/px; otherwise `mpp_override` is required.
- White fill for brightfield; black, or the file's colour, for fluorescence.
- `fileset_sha256` next to `sha256`.
- Formats validated only on synthetic samples need `experimental: true`.
