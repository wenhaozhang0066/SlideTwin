# Decision log

Every milestone adds its decisions here: what was decided, why, and the alternatives considered.
Pathology rules are never invented. Uncertain items are marked `draft` in the YAML configs and listed
under "Needs pathologist review" below.

## Needs pathologist review

All of these are `status: draft` until a pathologist confirms them. Record the reviewer and date when
confirmed.

| Item | Current draft value | Where |
|---|---|---|
| Magnification presets and keyboard keys | 1.25, 2.5, 5, 10, 20, 40x on keys 1-6 | `configs/profiles/*.yaml` |
| Magnification-to-resolution convention | `mpp(m) = 0.25 * 40 / m` um/px | profiles |
| Tolerance for offering a magnification | 0.06 (a nominal 40x scan at up to 0.266 um/px still offers 40x) | profiles |
| General reading habits R-G1 to R-G5 | as in SPEC 5.1 | `configs/protocols/sln_metastasis.yaml` |
| T1 thresholds | macro > 2 mm; micro > 0.2-2 mm and/or > 200 cells; ITC <= 0.2 mm and <= 200 cells. Which wording of the 200-cell boundary applies is open | protocol |
| Measurement rule | largest contiguous deposit only; satellites excluded; extranodal extension included | protocol |
| Negative coverage requirement | >= 98% of tissue seen at >= 10x, 32 um grid | protocol |
| Minimum pixels across a measurement | 20 px | protocol |
| Minimum magnification for positive evidence | 20x | protocol |
| Cost-model values | SPEC 8 placeholders; to be calibrated from pathologist logs (M7) | profiles |

## Needs user decision

| Item | Status |
|---|---|
| GitHub owner and visibility | private repository under the owner's account; waiting for a token |
| Hugging Face user or org and Space name | needed in M7 |
| CAMELYON license reading | stricter reading applied (see DATA_LICENSES.md) until confirmed |

## M0: scaffold (2026-10-14)

- **Repository.** `docs/SPEC.md` is the specification and `AGENTS.md` holds the working rules. Code,
  comments and docs are in English. `README_zh.md` and the UI strings are the only Chinese files, as SPEC
  requires.
- **Python 3.11+.** CI tests 3.11 and 3.12. The newest tiffslide (4.0), numpy (2.5) and tifffile need
  3.12; on 3.11 pip resolves tiffslide 3.0.x. The development venv and the Docker image use 3.12.
  - Alternative: require 3.12. Rejected because SPEC says 3.11+.
- **Build and tooling.**
  - hatchling, with the version taken from `slidetwin/__init__.py`.
  - ruff for lint and format; mypy strict on the whole package.
  - pytest with hypothesis.
  - A 100% line-and-branch coverage gate on `slidetwin/core`, as SPEC 6.2 requires. CI and
    `scripts/ci_local.sh` enforce it.
- **Test data.**
  - CI downloads only the CC0 `CMU-1-Small-Region.svs` (1.9 MB) and checks its sha256.
  - Tests that need it skip locally when it is missing. With `SLIDETWIN_REQUIRE_TESTDATA=1`, as in CI, a
    missing file fails the run instead.
  - Tests that need CAMELYON slides run only when `SLIDETWIN_CAMELYON_DIR` is set.
- **Versioning in logs.** `slidetwin.version.git_sha()` reads `SLIDETWIN_GIT_SHA` (set at Docker build
  time) or asks git, adding `-dirty` when tracked files have uncommitted changes.
- **Docker.**
  - `python:3.12-slim`, one uvicorn worker (sessions live in memory), uid 1000 as Hugging Face requires.
  - Slides are mounted or downloaded at runtime and never baked in.
  - The image is not built on the development cluster, which has no docker or apptainer. It is built
    and checked in M7.
- **Secrets.**
  - Tokens are read from environment variables only. `.gitignore` excludes `data/`, `cache/`, `logs/`,
    `.env`, `*.env`, virtual environments and whole-slide image formats.
  - Commits carry only the owner's identity.
- **License.** Apache-2.0, as SPEC suggests. The owner stated no preference. It can still be changed before
  the repository becomes public.

## M1: core engine (2026-10-15)

The binding contract is [ARCHITECTURE.md](ARCHITECTURE.md). Decisions made while building it:

- **D1 Read path.**
  - Use the coarsest pyramid level that is at least as fine as the target on both axes.
  - Read an integer-aligned window, padded for the Lanczos support.
  - Resample the exact fractional box with Pillow `resize(box=...)`.
  - If the chosen level is still at least 2x finer (a shallow pyramid), reduce by an integer box
    factor first.
  - Refuse reads above 400 MP.
  - Alternatives: OpenSlide `read_region` followed by a plain resize. Rejected because integer
    rounding of the origin shifts views by up to one source pixel.
- **D2 Magnification cap.**
  - A preset is offered iff `mpp(m) >= native_mpp * (1 - 0.06)`, where native is the coarser axis.
  - At 0.06, nominal 40x scanners up to 0.266 um/px still offer 40x, upsampling by at most 6%.
  - Anything finer fails with `MAG_UNAVAILABLE`, using the message SPEC 7 asks for.
  - The tolerance is a draft profile value (needs review).
- **D3 Clamping.**
  - The field of view stays inside the slide.
  - On an axis where the field of view is wider than the slide, the view is centred on that axis and
    the outside is `fill_rgb` (white).
  - Alternative: clamp only the centre. Rejected because it wastes view area on glass outside the
    slide and complicates coverage.
- **D4 Costs.**
  - The cost table in ARCHITECTURE.md reproduces SPEC 8 and appendix B exactly (a 0.5 pan costs
    0.575 s).
  - Pan distance is the actual displacement after clamping.
  - The initial view costs one render under `viewer` and is free under the flat model.
- **D5 Virtual clock.** Kept as integer microseconds, so replay is bit-exact.
- **D6 Views.**
  - Ids are per session (`v_0001`...).
  - Encoding is baseline JPEG, quality 90, 4:4:4. It is set in the profile, so it is covered by the
    profile hash.
  - The log stores the sha256 of the raw RGB pixels and of the JPEG.
  - Images go to an `ImageStore` (memory or disk) and can be fetched only by an id already served in
    that session.
- **D7 Crop (unconstrained only).**
  - The output long side equals the viewport long side, but never finer than the scan.
  - Pixels stay square; the returned bbox is adjusted by less than one pixel.
  - The crop becomes the current view, so measurement works on it, but it does not move the viewer
    state.
- **D8 Failures.**
  - A failed action changes no state, costs 0 and is still logged with its error code. Replay
    reproduces it.
  - When the budget is used up, only `submit_report`, `get_state` and `export_trajectory` run.
  - The action that crosses a time limit completes; the next one is refused.
- **D9 Hashes.**
  - Slide: sha256 of the whole file, cached by real path, size and mtime under `cache/`. A sha256
    pinned in the manifest is enforced.
  - Profile and protocol: sha256 of canonical JSON of the normalised model, so defaults count.
  - The full normalised profile and protocol are embedded in the log header, so a log replays without
    the config files.
- **D10 Replay.**
  - Re-runs every event on a fresh session.
  - Compares action, cost, virtual time, `state_after`, `view_id`, error code and the observation
    (pixel hashes included).
  - Ignores wall time, client metadata and JPEG bytes. Encoders may differ across Pillow builds; pixels
    may not.
- **D11 Associated images.**
  - Label, macro and embedded thumbnail images can carry patient data. `SlideSource` has no accessor
    for them.
  - The navigation thumbnail is always rendered from the pyramid.
- **D12 Ground-truth isolation.** The manifest's `eval_only` block is deleted before validation, and
  unknown manifest keys are rejected, so ground truth cannot reach a core object, response or log.
- **D13 Level geometry.**
  - Per-axis downsample is level-0 extent divided by level extent.
  - tiffslide is the reference backend: reads go straight to the zarr level arrays in level
    coordinates.
  - OpenSlide is optional. Its `read_region` takes level-0 coordinates and its averaged downsample,
    which can shift a read by a sub-pixel on non-power-of-two pyramids. Replay therefore records the
    backend.
- **D14 Pyramid quantisation.**
  - A pyramid level is a block average, so a feature smaller than a level pixel is located only to
    that pixel. This is inherent to WSIs, not a renderer error.
  - Tests check sub-pixel placement with features symmetric about power-of-two boundaries, which keep
    their centroid at every level.
- **D15 Measurement coordinates.** Points are continuous view-pixel coordinates, where pixel `k` spans
  `[k, k+1)`, inside `[0, w] x [0, h]`. They are converted with the view's own bbox and mpp.
- **D16 Task description.**
  - `create_session` returns the full protocol: rules, thresholds, coverage requirement, hints and
    report fields, with `status: draft`.
  - Humans and agents are judged by the same rules, so both see them. It contains no ground truth.
- **D17 Protocol file.** Appendix A was kept verbatim. Added fields: `title`, `instructions`,
  `general_rules` (R-G1 to R-G5 from SPEC 5.1) and `category_notes` (the open 200-cell wording). All
  are `draft`.
- **D18 Input limits.**
  - Actor: `human:` or `agent:` plus 1 to 64 characters from `[A-Za-z0-9._-]`. These are pseudonyms;
    no personal data.
  - Viewport: 64 to 4096 px per side.
  - Client metadata: at most 4 KB of JSON, never on the clock.
  - A log file is opened exclusively, so a session never appends to another session's log.
- **Python 3.11 support.** tiffslide 4.0, numpy 2.5 and tifffile 2026 need Python 3.12. On 3.11, pip
  resolves tiffslide 3.0.x, which CI covers.
