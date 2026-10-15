# SlideTwin: plan for M0-M2 (awaiting approval)

Status: draft, 2026-10-14. No code has been written yet. Approving this plan starts M0.
Decisions marked D* go into `docs/DECISIONS.md` in the milestone where they are implemented.

## 1. Environment findings (checked 2026-10-14)

| Item | Finding | Consequence |
|---|---|---|
| Python | System Python 3.11 and 3.12; `venv` works | Dev venv at `SlideTwin/.venv` (Python 3.12, gitignored) |
| Disk | Home 94% full (11 GB free); scratch over quota; /project has 3.2 TB free | Venv, pip cache, logs and caches stay on /project |
| Network | PyPI, GitHub API, OpenSlide test data, CAMELYON S3 all reachable | No blocker |
| Tools | git 2.43, node 22. No `gh`, docker or apptainer on this node. No GitHub or HF token in env. No git identity configured | Dockerfile is written but cannot be built here (the HF Space builds it in M7). `gh` is installed locally once a remote is approved |
| CI slide | OpenSlide `Aperio/CMU-1-Small-Region.svs`: 1.9 MB, CC0, 2220x2967 px, one level, 0.499 um/px (20x scan). sha256 `ed92d5a9...c8a7` | Covers the "40x unavailable" path. Its label and macro images must never be served |
| Demo data | CAMELYON16 complete on disk: 399 TIFF (270 training, 129 test), 160 ASAP XML, masks, `reference.csv` | No download needed for local work |
| mpp | All CAMELYON16 files are Philips TIFF. mpp is stored only in the Philips XML; there are no TIFF resolution tags. tiffslide 4.0.0 reads it correctly (0.2263 / 0.2431 um/px, 8-10 levels) | tiffslide stays the default backend |

## 2. M0: scaffold

- Repo root: the project directory on the development cluster. Move `SPEC.md` to `docs/SPEC.md` (AGENTS.md already points there). Run `git init` on `main`.
- `pyproject.toml` (hatchling, package `slidetwin`, Python >= 3.11).
  - Runtime: fastapi, uvicorn, pydantic>=2, numpy, pillow, pyyaml, tiffslide.
  - Extras: `openslide` (openslide-python, openslide-bin), `mcp` (added in M4), `dev` (pytest, pytest-cov, hypothesis, ruff, mypy, httpx, types-PyYAML).
- Ruff (lint + format) and mypy (strict on `slidetwin/core`). Coverage gate: 100% line + branch on `slidetwin/core`, as SPEC 6.2 requires.
- `slidetwin/api/main.py`: `GET /api/health` returns `{status, version, git_sha}`.
- `.github/workflows/ci.yml`: Python 3.11 and 3.12; runs ruff, mypy and pytest. It downloads only the CC0 test slide, checks its sha256 and caches it.
- `scripts/ci_local.sh` runs the same checks locally until a remote exists.
- Docs:
  - `docs/DECISIONS.md`, with sections "Needs pathologist review" and "Needs user decision".
  - `docs/DATA_LICENSES.md`.
- Repo files:
  - `README.md` with the HF Space YAML header and the research-only notice.
  - `README_zh.md`. SPEC requires it; it and the UI strings are the only non-English files.
  - `LICENSE`, a stub `Dockerfile`.
  - `.gitignore` covering `data/ cache/ logs/ .env .venv/` and slide formats.
- Acceptance: `pytest -q`, `ruff check .` and `mypy slidetwin` pass locally. "CI green" needs a GitHub remote (question Q1). Until one exists, I report the local run of the same CI script.

## 3. M1: core engine (geometry tests written before the code)

Modules (`slidetwin/core/`, no FastAPI imports):

- `geometry.py`: pure functions.
  - `mpp(m) = mpp_ref * 40 / m` and magnification availability.
  - Level choice, viewport bbox in um, clamping, um<->px for a view.
- `slide_source.py`: `SlideSource` protocol covering level-0 size, native mpp (x, y), levels and `read_region`.
  - Implementations: `TiffSlideSource` (default), `OpenSlideSource` (optional) and `SyntheticSlideSource` (tests).
  - Associated images (label, macro, thumbnail) are never exposed, because they can carry patient data.
- `registry.py`: loads the slide manifest. Paths resolve under `SLIDETWIN_SLIDE_DIR`, so the repo holds no internal paths.
  - Refuses a slide without mpp unless `mpp_override` is set with a stated source.
  - Verifies the sha256 when the manifest pins one.
  - Ground-truth fields live under `eval_only:` and the core loader drops them.
- `render.py`: the `ViewerBackend` seam (SPEC 6.3), with `render_view`, `render_thumbnail` and `render_crop` for a local `SlideSource`.
- `ledger.py`, `session.py`, `trajectory.py` (writer, reader, replay) and `config.py` (loads and hashes profiles and protocols).
- Configs:
  - `profiles/viewer.yaml` and `profiles/unconstrained.yaml`.
  - `protocols/sln_metastasis.yaml`: Appendix A verbatim with `status: draft`. M1 loads and hashes it; rule checks arrive in M5.
  - `slides/manifest.yaml`: the CI slide plus the demo slides chosen in Q5.

Engineering decisions (tell me if you disagree):

- **D1 Read path.**
  - Pick the coarsest level whose mpp is <= the target mpp, then read an integer-aligned window.
  - Resample the exact fractional bbox with Pillow `resize(box=...)` and Lanczos, so placement is exact to the sub-pixel.
- **D2 Magnification cap.** Magnification m is available iff `mpp(m) >= native_mpp * (1 - tol)`.
  - Default `tol = 0.06` (draft), so nominal-40x scanners up to 0.266 um/px qualify (for example 0.263 um/px scanners).
  - Anything finer is refused with a specific message. Otherwise the code never upsamples.
- **D3 Clamping.** The viewport stays inside the slide.
  - If the field of view is larger than the slide on an axis, the view is centred on that axis and the outside is filled with a fixed neutral colour.
  - Alternative: clamp only the centre to the slide extent.
- **D4 Costs (viewer).** These reproduce SPEC 8 and Appendix B exactly (pan 0.5 gives 0.575 s).
  - pan = `pan_base + pan_per_viewport * d + view_render`, where d is the actual (post-clamp) displacement in viewport units.
  - move_to = `move_to + view_render`; set_magnification = `set_magnification + view_render`.
  - thumbnail, measure and annotate use their SPEC 8 costs. get_state, export and submit cost 0.
  - The initial view at create_session costs `view_render`.
  - In `unconstrained`, the thumbnail is free and every other action costs one flat step.
- **D5 Clock.** Virtual time is kept as integer microseconds, so replay is bit-exact.
- **D6 Views.**
  - Ids run `v_0001`... per session.
  - Encoding is JPEG quality 90 with 4:4:4 subsampling. This is set in the profile, so it is covered by the profile hash.
  - Images are written to `logs/sessions/<id>/views/`, not kept in RAM.
  - The log stores the sha256 of the raw RGB pixels and of the JPEG.
- **D7 Crop (unconstrained).**
  - The output long side equals the viewport long side, but never finer than native; in that case the output is smaller.
  - The crop is clamped to the slide and the actual bbox is returned.
- **D8 Budget.**
  - Checked before each action. Once the budget is used up, only submit_report, get_state and export_trajectory are allowed.
  - Rejected actions are logged with an error code and zero cost, so replay reproduces them.
- **D9 Hashes.**
  - Slide: sha256 of the file, computed once and cached by path, size and mtime.
  - Profile and protocol: sha256 of the canonical JSON of the parsed YAML.
  - The log header also records the backend name and version.
- **D10 Replay.** Re-run the logged actions on a fresh session and compare state_after, costs and the per-view pixel hashes.

Tests:

- Hypothesis properties:
  - um<->px round trip.
  - bbox size = viewport * mpp.
  - Level choice never upsamples beyond tol.
  - Clamping invariants.
  - pan followed by the inverse pan returns to the start away from edges.
  - Magnification cap.
- Synthetic pyramidal TIFF (tifffile, lossless) with markers at known um positions:
  - Each marker lands at the expected view pixel (+-1 px) at every magnification.
  - A known distance is measured exactly.
- CMU-1-Small-Region: 40x is refused with the specific message; 20x works.
- Ledger determinism, trajectory round trip, and replay pixel-hash identity.
- Acceptance script `scripts/demo_m1.py`: create session, set magnification, pan, view, export, then replay and check the result is identical. It runs on the CI slide and locally on one CAMELYON16 slide.

## 4. M2: REST API

All endpoints call the core `SessionManager` only. The MCP tools in M4 will use the same layer.

| Endpoint | Action |
|---|---|
| `POST /api/sessions` | create_session (slide_id, profile, protocol, viewport_px, actor) |
| `GET /api/sessions/{id}/state` | get_state (no pixels, no cost) |
| `POST /api/sessions/{id}/thumbnail` | get_thumbnail |
| `POST /api/sessions/{id}/magnification` | set_magnification |
| `POST /api/sessions/{id}/pan` | pan |
| `POST /api/sessions/{id}/move_to` | move_to |
| `POST /api/sessions/{id}/view` | view |
| `POST /api/sessions/{id}/measure` | measure |
| `POST /api/sessions/{id}/annotations` | annotate |
| `POST /api/sessions/{id}/crop` | crop (`unconstrained` only) |
| `POST /api/sessions/{id}/report` | submit_report (closes the session) |
| `GET /api/sessions/{id}/trajectory` | export_trajectory (JSONL) |
| `GET /api/sessions/{id}/views/{view_id}.jpg` | Image of a view already served in this session |
| `GET /api/slides`, `/api/profiles`, `/api/protocols` | Ids and public metadata only (no labels, no images) |

- Actions that cost time are POST.
- Responses carry `{state, observation, cost_s, elapsed_s, rule_violations}`. `include_image=false` returns only the view URL, which is what the web UI will use.
- Errors are 4xx with `{error: {code, message, hint}}`. For example, `MAG_UNAVAILABLE`: "40x is not available: this slide's native resolution is 0.50 um/px, so the highest magnification is 20x."
- Session store:
  - Sessions live in memory with an idle TTL of 2 h and a maximum of 16 sessions; both are set by env.
  - Trajectories are appended to disk in real time.
  - A lock per session and per slide handle keeps concurrent reads safe.
- `submit_report` in M2 validates the report (pydantic plus `report_schema.json`) and closes the session. Rule checks E1-E4 and coverage arrive in M5.
- `actor` must be `human:<alias>` or `agent:<name>`.
- Acceptance:
  - Contract tests for every endpoint: success, error codes, costs, budget, closed session, crop refused under `viewer`, view images only for the session's own views, and no label leakage.
  - `/docs` and `/openapi.json` are reachable.

## 5. Needs pathologist review (initial list, all `draft`)

- Magnification presets and keys, `mpp_ref_40x = 0.25`, `tol = 0.06`.
- R-G1 to R-G5.
- T1 thresholds, including which wording of the 200-cell ITC boundary applies.
- Measurement rule (satellites, extranodal extension).
- Negative coverage of 98% at >=10x on a 32 um grid.
- Minimum of 20 px across for a measurement; confirmation at >=20x.
- All cost-model values.

## 6. Data note (for M6, not blocking)

In the CAMELYON16 `reference.csv`, the label and the annotation geometry disagree on 17 training slides, using the maximum Feret diameter of the largest single annotated polygon:

- 15 of the 58 slides labelled `macro` have no polygon larger than 2 mm. For example, tumor_010 measures 0.28 mm.
- 2 of the 53 slides labelled `micro` have a polygon of 2.03 mm (tumor_083, tumor_106).

Either one deposit is drawn as several polygons, or the label follows a different rule. The measurement ground truth needs a stated definition, for example merging touching polygons. The proposed demo slides below are ones where the label and the geometry agree.

## 7. Questions (SPEC 18 plus extras); defaults in brackets

1. Project name, GitHub account or org, public or private, and whether to create the remote now. [SlideTwin / `slidetwin`; private until M7]
2. HF user or org and Space name. [Only needed in M7]
3. Code license. [Apache-2.0]
4. Align magnification presets and shortcuts with the company viewer? [SPEC defaults 1.25-40x, keys 1-6, as draft]
5. Demo slides and default UI language. Proposed slides, all from the CAMELYON16 training set (about 3.9 GB):
   - tumor_082: macro, largest deposit about 4.1 mm.
   - tumor_093: micro, about 1.3 mm.
   - tumor_081: micro, about 0.28 mm, close to the ITC limit.
   - normal_108: negative.
   - Optional: tumor_058, a macro on the other scanner (3DHistech), adding 1.4 GB.

   [UI language follows the browser, with a toggle]
6. Viewport. [1024x768]
7. Timer. [Hidden by default; switchable at session setup]
8. Git commit author name and email. [the owner's identity]
9. Pace: AGENTS.md says to stop after each milestone for review. Stop after each of M0, M1 and M2, or run M0-M2 straight through and report once?
