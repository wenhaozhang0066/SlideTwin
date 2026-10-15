# Decision log

Every milestone adds its decisions here: what was decided, why, and the alternatives considered.
Pathology rules are never invented. Uncertain items are marked `draft` in the YAML configs and listed
under "Needs pathologist review" below.

## Needs pathologist review

Each protocol rule in `configs/protocols/sln_metastasis.yaml` has its own `status`, `reviewed_by` and
`reviewed_on`. A pathologist confirms rules one at a time; `confirmed` requires a reviewer name and
date, and the protocol itself can be confirmed only when every rule is. A test keeps this table in
step with the YAML.

| Rule | Current draft content |
|---|---|
| `general_rules.R-G1` | Low-power overview first (thumbnail or <= 2.5x). |
| `general_rules.R-G2` | Systematic serpentine screening at 5x-10x. |
| `general_rules.R-G3` | Confirm suspicious areas at 20x-40x before using them as evidence. |
| `general_rules.R-G4` | Measure with the tool within one view, in um or mm. |
| `general_rules.R-G5` | Negative conclusions must meet the coverage requirement. |
| `task_rules.T1-SCOPE` | Every tissue piece (several node sections per slide) must be examined. |
| `task_rules.T1-ITC-REPORT` | Report ITC separately only when there is no macro- or micrometastasis. |
| `magnification` | Screening at >= 10x; positive evidence at >= 20x. |
| `measurement` | Largest contiguous deposit only; satellites excluded; extranodal extension included; >= 20 px across. |
| `coverage` | >= 98% of tissue seen at >= 10x on a 32 um grid (placeholder numbers); tissue = median-filtered saturation > 12, pieces >= 0.01 mm2 (misses very pale fat on Radboud slides). |
| `categories.macro` | Macrometastasis: > 2 mm. |
| `categories.micro` | Micrometastasis: > 0.2 mm to 2 mm and/or > 200 cells. Which edition's 200-cell wording applies is open. |
| `categories.itc` | Isolated tumour cells: <= 0.2 mm and <= 200 cells (same open wording question). |
| `reading_path_hints` | The four SPEC 5.2 hints (never enforced). |
| `reporting` | Fields: metastasis, category, largest deposit (mm), extranodal extension. |

How the rule checks read these values (see M5 below) also needs review: the E3 size and category
logic, `absent` paired with category `none`, and the E4 scopes.

Profile values (`configs/profiles/*.yaml`, profile-level `status: draft`):

| Item | Current draft value |
|---|---|
| Magnification presets and keyboard keys | 1.25, 2.5, 5, 10, 20, 40x on keys 1-6 (keys arrive with the UI in M3) |
| Magnification-to-resolution convention | `mpp(m) = 0.25 * 40 / m` um/px |
| Tolerance for offering a magnification | 0.06 (a nominal 40x scan at up to 0.266 um/px still offers 40x) |
| Default viewport | 1280 x 1024 px (see "Default viewport" below); a pathologist may prefer a larger field |
| Cost-model values | SPEC 8 placeholders; to be calibrated from pathologist logs (M7) |

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

## M2: REST API (2026-10-15)

- **One route per SPEC 7 action.** Each route calls exactly one `SessionManager` or `Session` method:

  | Action | Route |
  |---|---|
  | create_session | `POST /api/sessions` |
  | get_state | `GET .../state` |
  | get_thumbnail | `POST .../thumbnail` |
  | set_magnification | `POST .../magnification` |
  | pan | `POST .../pan` |
  | move_to | `POST .../move_to` |
  | view | `POST .../view` |
  | measure | `POST .../measure` |
  | annotate | `POST .../annotations` |
  | crop | `POST .../crop` |
  | submit_report | `POST .../report` |
  | export_trajectory | `GET .../trajectory` |

  - Actions that cost time or change state are POST.
  - `get_state` is a GET, costs 0 and is still logged.
- **Images.**
  - Responses embed the JPEG as base64 by default (agents); `include_image=false` returns only its
    URL (the web UI).
  - `GET .../views/{view_id}.jpg` and `.../thumbnails/{thumb_id}.jpg` serve only images that session
    was already served and logged.
  - There is no tile, region or slide-image endpoint.
- **Catalogue.**
  - `GET /api/slides` lists manifest ids with license, attribution and source only. Opening slides
    would hash gigabyte files, and labels are never listed.
  - `GET /api/profiles` returns the full rules (costs, presets, budgets).
  - `GET /api/protocols` returns the task descriptions.
  - `create_session` also returns `slide`, `task` (protocol) and `rules` (profile), so humans and
    agents see the same rules.
- **Errors.**
  - One envelope: `{"error": {"code", "message", "hint", "details"}}`.
  - Status by code:

    | Status | Codes |
    |---|---|
    | 400 | parameter and magnification errors, outside view |
    | 403 | action not allowed by the profile |
    | 404 | unknown session, image or slide |
    | 409 | session closed, budget used up, no current view |
    | 413 | body larger than `SLIDETWIN_MAX_BODY_BYTES` (1 MB) |
    | 422 | `INVALID_REQUEST` (schema), report invalid, slide metadata |
    | 503 | too many sessions |
    | 500 | config or integrity errors; `INTERNAL`, with no details leaked and the server log keeping the trace |

  - Request models forbid unknown fields and non-finite numbers. The core validates again.
- **Session store.**
  - Sessions live in memory with a 2 h idle TTL and a limit of 16. When the limit is reached, closed
    sessions are evicted first.
  - Creation is serialised so the limit holds.
  - Session ids are random 128-bit hex. They act as capability tokens; there are no user accounts
    (SPEC 2).
  - Trajectories and served images are written to `SLIDETWIN_LOG_DIR/sessions/` as they happen. An
    expired session's trajectory can still be downloaded; its images are not served.
- **Body limit.** A pure ASGI middleware buffers at most `max_body_bytes` before the app reads the
  body. FastAPI would otherwise turn a stream error into a 400.
- **Settings.**
  - Environment variables only: `SLIDETWIN_CONFIG_DIR`, `_MANIFEST`, `_SLIDE_DIR`, `_LOG_DIR`,
    `_CACHE_DIR`, `_MAX_SESSIONS`, `_SESSION_TTL_S`, `_MAX_BODY_BYTES`.
  - `slidetwin.api.main:app` is built on first access, so importing the module has no side effects.
- **Deferred.**
  - Rate limiting and CORS: rate limiting arrives with the public Space (M7). The web UI (M3) is
    same-origin.
  - Rule checks E1-E4 and coverage in the report receipt: M5.

## Adversarial review of M1 and M2 (2026-10-15)

Two independent reviews, each finding re-checked by a skeptic:
- Core: four angles (geometry and rendering, SPEC compliance, determinism and replay, robustness and
  security).
- REST layer: three angles (contract, security, robustness).

Results: 57 findings confirmed (2 critical), 12 refuted. Every confirmed finding is fixed. Each has a
regression test that names its review id, in `tests/core/test_review_regressions.py`,
`tests/api/test_review_regressions.py` and `tests/core/test_error_paths.py`. Decisions that changed:

- **Ground truth through slide ids (critical).**
  - Problem: the demo ids repeated the CAMELYON file names, which contain the label
    (`c16_tumor_082`), and every session returns its slide id.
  - Ids are now opaque: `c16_` plus the first six hex digits of the file's sha256.
  - The manifest loader refuses an entry whose id, license or attribution contains any `eval_only`
    value.
  - `GET /api/slides` lists only id, license and attribution; the source URL stays on the server.
  - Errors name the slide id, never the file. This supersedes D12.
  - Limitation: a public slide can be identified from its dimensions or file hash. Blind experiments
    need offline agents or non-public slides.
- **Atomic actions.**
  - Every action plans without side effects, then commits with a full rollback on any failure. Roll
    back means no id is used up and nothing is charged.
  - Unexpected failures become `INTERNAL` and are logged.
  - A session that cannot write its log stops. Replay skips `INTERNAL` events, which record environment
    failures that applied nothing.
  - This supersedes the failure wording of D8.
- **Logged parameters.**
  - Successful events log the canonical values the action used, so they always replay.
  - Failed events log a JSON-safe copy of the raw input, capped at 16 KB.
  - Annotation geometry must have exactly `type` and `coordinates`.
  - Error messages echo inputs in shortened form only.
- **Log encoding.** Lines are ASCII JSON, read by splitting on `\n` only, and partial lines are cut
  back. U+2028, U+2029, U+0085 and lone surrogates can no longer break a trajectory.
- **Client metadata** never decides an action: invalid or oversized metadata is replaced by a note.
  Replay ignores client metadata, so this keeps every log replayable.
- **`close()` is final.**
  - It waits for a running action, then refuses everything.
  - The manager closes sessions outside its own lock and deletes the images of ended sessions;
    trajectories stay.
- **View budget.** It limits only actions that would serve a new view. The last view can still be
  measured and annotated. This supersedes the budget wording of D8.
- **Per-rule review (SPEC 5).**
  - Every rule block, each text rule and each category has `status`, `reviewed_by` and `reviewed_on`.
    `confirmed` requires a reviewer and a date, and the protocol is confirmed only when every rule is.
  - Structure changes from appendix A, which supersede D17's "verbatim":
    - categories are a list of per-category rules with `limits`;
    - `report_fields` became `reporting.fields`;
    - the scope rule and the ITC reporting rule became `task_rules`.
  - A test keeps the "Needs pathologist review" table complete.
- **Configs.** Protocol numbers must be finite.
- **Thumbnail.** It is never finer than the scan; small slides get small thumbnails.
- **Rendering.**
  - A partial block at the slide edge is completed with fill before integer reduction, so the edge no
    longer moves.
  - Reduction and padding are per axis, with a 100 MP canvas guard.
- **Backends.**
  - Open and decode failures become `SLIDE_METADATA` or `SLIDE_READ_ERROR`, naming the slide id.
  - Handles are closed on failure.
  - `open_slide` defaults its display name to `"slide"`, never to the file name.
- **Slide hash cache.**
  - The cache is only a hint: malformed content and write failures are ignored, and temp files are
    unique.
  - A pinned hash is always checked on the file itself, once per process.
- **Replay.** Header and event structure are validated; damaged logs give `REPLAY_ERROR`.
- **Strict input.**
  - Report models are strict.
  - Actor, image and session ids use exact ASCII patterns.
  - REST numbers no longer accept booleans or numeric strings.
- **REST.**
  - Errors carry `seq`, `cost_s` and `elapsed_s`.
  - Undecodable bodies give 422 `INVALID_REQUEST`.
  - 405 responses keep `Allow` and name the method to use; 404 points to `/docs`.
  - Every route documents its own error statuses, including 500.
  - The report schema is inlined in OpenAPI and served at `GET /api/report_schema`.
  - Payloads are built by core methods (`Session.opening()`, `SessionManager.catalogue()` and others),
    so MCP will serve the same.
- **Server settings.**
  - Settings are validated, and `SLIDETWIN_LOG_DIR` is required and checked at startup. A memory-only
    server could lose finished trajectories.
  - New `SLIDETWIN_SESSION_MAX_AGE_S` (default 8 h).
  - Reading state, images or the trajectory does not keep a session alive.
  - 503 responses carry `Retry-After`.
  - Invalid creations are refused before any slide is opened or folder created.
  - Slides open under per-slide locks, and `/api/health` is async.

Refuted, with the reason they were left unchanged:

| Findings | Reason |
|---|---|
| GR-4, GR-5 | The crop cap stays at native resolution, and level choice stays strict. Upsampling is never used when avoidable; the tolerance applies only to offering presets. |
| SC-06 | Report fields from YAML drive rule checks in M5. |
| SC-09 | Each event already carries its time. |
| DR-07, DR-13 | Replay proves reproducibility, not authenticity. A hash chain can be added if needed. |
| DR-09 | Header fields outside the hashed content are informational. |
| RS-10 | Images live in a server-local store. |
| M2-C3 | A request refused by the REST schema is a transport error, not a session action. |
| M2-C10 | Listing only openable slides would need hashing gigabyte files at startup. |
| M2-SEC-06 | Request timeouts and rate limits come with the public Space (M7). |

## Default viewport (2026-10-15)

- **Decision.** 1280 x 1024 px, shown 1:1, in both profiles, for humans and agents alike.
- **Source.** Japanese Society of Pathology and Japanese Society of Digital Pathology, *Technical
  Standards of Digital Pathology Systems for Pathologic Diagnosis*, 4th ed., 2024-05-17
  (https://pathology.or.jp/kijun_v4.pdf; the clauses were checked against the PDF).
  - Section 8.1(1): the monitor has at least 1280 x 1024 display pixels.
  - Section 8.2(3): display at 1:1 pixel scale is the basis.
- **Why this source.** It was the only normative pixel requirement found. FDA guidance, CAP and DICOM
  set no pixel count. ESP and SEAP suggest Full HD as a minimum, and FDA-cleared systems ship 1920 x
  1200 to 3840 x 2160 displays.
- **Field of view.**
  - 40x: 320 x 256 um (0.082 mm2, about a third of an FN 22 high-power field).
  - 10x: 1.28 x 1.02 mm.
  - 1.25x: 10.2 x 8.2 mm.
- **Agents.**
  - Claude 4.7 or later, Qwen3-VL and gpt-5.4-class models at high detail take 1280 x 1024 unscaled.
    GPT-4o-class tile models shrink it to 960 x 768.
  - A client that must downscale does so itself, maps coordinates back to view pixels and declares the
    scale in client metadata.
  - Other sizes run only as named conditions for humans and agents alike:
    - 1920 x 1080: clinical display.
    - 2560 x 1440: about one HPF at 40x.
- **Alternatives.**
  - 1024 x 768 (the SPEC suggestion): below the JSP minimum.
  - 1920 x 1080: an expert-opinion minimum.
  - 2560 x 1440: one cleared system.
- **For M3.** The UI shows the view at 1:1 device pixels (aware of devicePixelRatio) and logs the display
  scale of each human session.

## M5: rules and coverage (2026-10-15)

- **Coverage grid.** Square cells of `coverage.grid_um` (32 um) from the level-0 top-left corner.
  - A cell counts as seen when its centre lies inside a view of at least `coverage.at_or_above_mag`.
    Crops count at their equivalent magnification.
  - Coverage is seen tissue cells over tissue cells in scope. A scope without tissue counts as
    covered (1.0).
- **Tissue mask.** Built from the slide pixels at grid scale, never from annotations:
  - median-filtered (3x3) HSV saturation above `coverage.tissue_min_saturation` (12 on 0-255);
  - then closing (2 cells), hole filling, and pieces under `coverage.tissue_min_area_um2` (0.01 mm2)
    dropped.
- **Why not Otsu (the SPEC 13 example).** Measured on the demo slides:
  - glass saturation is 3-7, while Otsu picked thresholds of 40-78, so only dense nodal tissue
    counted and the pale perinodal fat and stroma were left out;
  - an optical-density criterion caught more pale tissue but also the scanner frame on the Radboud
    slide, which would make 98% coverage unreachable.
  - Known limitation: the very pale fat around nodes on Radboud (3DHistech) slides is still missed.
    The parameters live in the protocol YAML (needs pathologist review).
- **Tissue pieces.** 8-connected components, numbered by descending area (piece 1 is the largest).
  `get_thumbnail` returns them (id, bbox, area) and logs them with the thumbnail, so a negative claim
  can cite `tissue_piece:N`. They are derived from the slide at grid scale, never from ground truth.
- **Rule checks (SPEC 5.4).** Statuses are `pass`, `fail`, `not_applicable` and `not_verifiable`.
  - E1: every cited view or measurement id belongs to the session.
  - E2: each finding has an evidence view at `magnification.confirm_min` or higher.
  - E3: each measurement spans at least `measurement.min_pixels_across` view pixels, and the category
    agrees with the size.
    - The size is the largest cited measurement, otherwise the reported `largest_deposit_mm`.
    - Category limits are read from the YAML: `gt_mm` and `le_mm` are checked. `or_cells_gt` makes a
      size mismatch `not_verifiable`, because cell counts cannot be measured here; `cells_le` is not
      checked.
    - `absent` must come with category `none`, and `present` with another category.
  - E4: `metastasis: absent` and every negative claim need `coverage.negative_min_fraction`.
    Scopes: the whole slide, `tissue_piece:N` or `region:x0,y0,x1,y1` (um).
  - These interpretations need pathologist review.
- **Enforcement.**
  - `advisory`: the report is accepted. The receipt lists every check, the number of violations and the
    coverage (whole slide and per piece), and the event's `rule_violations` lists the failures.
  - `enforced`: the report is refused with `RULE_VIOLATION` (422), which carries the failed checks; the
    session stays open, so the reader can look further and submit again.
- **Tissue map caching.** Cached per open slide source, which holds one entry per slide in the
  registry, and dropped with it.

## M3: web UI (2026-10-15)

- **No framework, no build step.** Plain ES modules and Canvas: there is nothing to install or bundle,
  and the page is small enough to read in full. Alternatives were React or Svelte with a bundler, which
  add a toolchain for a page with one canvas and one form.
- **Same-origin page under a strict Content Security Policy.**
  - No CDN, web font or analytics: a reader's browser makes no third-party requests, and the UI works
    offline.
  - A side effect: Playwright's `wait_for_function` uses `eval`, which the policy blocks. The browser
    tests wait on locators instead and keep the policy on.
- **Display at one device pixel per view pixel** when the window allows (JSP 8.2); otherwise the view
  shrinks to fit and is never enlarged. The scale used is logged with every action.
- **A drag sends one `pan` on release** (SPEC 10.1). The image follows the pointer with a CSS
  translation, but no pixels are fetched during the drag: live panning would have to show unlogged
  pixels or log dozens of pans per gesture.
- **Overview.** The thumbnail is fetched once when the session starts, if the profile allows it. It is
  logged with `input: auto` and costs `thumbnail_s` like any thumbnail.
  - Tissue pieces are numbered only where their box on the overview is large enough to hold a number;
    the piece chosen as the scope of a negative statement is outlined.
- **Keyboard.** SPEC 10.2 asks that everything work from the keyboard.
  - Keys 1-6 select presets in the profile's order. W/A/S/D pan half a viewport, and the arrow keys
    pan too while navigating. N, M, P and G choose a tool. Esc cancels.
  - With a tool, the arrow keys move a cursor on the view (10 px, or 50 px with Shift). Space places a
    point there; Enter finishes a polygon.
  - On the focused overview, the arrow keys move a cursor and Enter moves the view there.
  - Shortcuts are ignored while typing in a field and when Ctrl, Alt or Meta is held.
- **Report form.**
  - No answer is preselected: each conclusion starts at "Choose...", and the browser stops a submit
    with missing fields before any request is sent, so the log has no failed attempt.
  - A preselected answer could anchor the reader and would let an unread form be submitted.
  - Claims are added from the current view (finding), the last measurement, or a scope (whole slide or
    a tissue piece), and each needs a statement.
  - The receipt shows every rule check and the coverage. An enforced refusal lists the failed checks,
    and the form stays open.
- **Session end.** After an accepted report the controls are disabled and the trajectory link stays.
- **Language.** It follows the browser and can be switched. Only the UI strings exist in Chinese; code
  and documents stay in English (AGENTS.md).
- **Tests.**
  - `node --test tests/web/*.test.mjs`: geometry, string keys in both languages, report building.
  - `tests/api/test_web.py`: static routes, and the check that the UI calls only `/api/` routes.
  - `tests/e2e`: headless Chromium (Playwright). A full reading with keys, drag, overview (click and
    keyboard), measuring (pointer and keyboard), annotations and a report, then the downloaded
    trajectory replays identically. Two smaller tests cover a small window and a bad pseudonym.
  - Chromium runs on the cluster from `.tools/pw-browsers` (gitignored). CI installs it in a separate
    `web` job. The e2e tests skip without Playwright unless `SLIDETWIN_REQUIRE_E2E=1`.

## M4: MCP and baseline agents (2026-10-15)

- **Official SDK, version 2** (`mcp` 2.3: `MCPServer`, streamable HTTP), as SPEC 6.1 asks.
  - It depends on `httpx2`, the successor of `httpx`. Starlette's `TestClient` now uses it, so
    `httpx` left the dev dependencies.
- **Tools mirror the actions one to one.** They return the REST JSON as text and as structured
  content, plus the served JPEG as image content.
  - A refused action is an `isError` result carrying the REST error envelope, so agents handle both
    transports the same way.
  - Argument type errors come from the SDK's schema check, as plain text, and are not logged, like
    a REST 422.
- **`/mcp` is a route, not a mount.**
  - A mount at `/mcp` answers `/mcp` with a redirect to `/mcp/`. Behind a TLS proxy the redirect can
    point to `http://`, and a POST does not survive it.
  - A mount at `/` would send every unknown path to the MCP app instead of the REST 404 envelope.
  - The SDK's route is added to the app's router, and the app's lifespan runs the SDK session
    manager (a mounted app's own lifespan never runs).
- **Stateless JSON responses.** Every tool takes the environment session id, so MCP needs no
  transport session. Plain JSON also passes proxies that buffer event streams (the Space).
- **Host and Origin checks on `/mcp`** (DNS rebinding protection). They are on by default for the
  localhost forms. A deployment sets `SLIDETWIN_MCP_ALLOWED_HOSTS` to its host name, or `*`.
- **Logs.** MCP actions are logged with `client = {"via": "mcp"}`. A test runs the same actions
  through REST and MCP and finds the same parameters, costs, states, pixel hashes and errors.
- **Agents are clients.** They never import the core. `Env` gives one interface over REST and MCP,
  so each agent runs unchanged on either.
- **Serpentine agent.**
  - It plans fields that tile the slide at the 10x field size, keeping those that hold tissue in its
    own thumbnail mask (saturation, dilated by two thumbnail pixels). It visits them row by row in
    alternating directions: a `pan` when the next field is within about one viewport, otherwise
    `move_to`.
  - A colour heuristic flags fields where at least 30% of the tissue pixels are dense purple. The 3
    highest are looked at again at 20x, and the visible extent of the largest purple region is
    measured.
  - The report is `indeterminate` when anything was flagged, else `absent`. It always adds a
    negative claim for each tissue piece without a flag; that claim is the coverage proof.
  - On the demo slides: `cmu1_small` needs 2 fields and reaches 100% coverage. `c16_aba3ae` needs
    227 fields: 100% coverage, all checks passed, 83 s of wall time and 169 s of virtual time.
  - The heuristic is not a diagnostic method. Lymph node tissue is purple almost everywhere, so most
    fields are flagged.
- **Random agent.** Seeded random moves and a random conclusion: the lower bound.
- **Crop agent.** Thumbnail plus up to six 400 um crops of the most purple tissue blocks, under the
  `unconstrained` profile only. Its findings cite the crops' view ids.
- **LLM agent example.**
  - Six tools: thumbnail, magnification, pan, move_to, measure, report. The report tool's input
    schema is the report schema itself. Views return as images inside the tool results.
  - Anthropic backend: the official SDK, model `claude-opus-5-5` at effort `medium` (that model's
    default; thinking is always on).
    - The loop never edits the transcript, because thinking blocks are tied to the conversation
      they came from.
    - Prompt caching is automatic on the growing prefix.
    - A declined request is retried server-side on Anthropic's recommended fallback model
      (`fallbacks: "default"`). This is on by default and can be removed.
  - OpenAI-compatible backend (e.g. vLLM with a Qwen-VL model): plain HTTP. Tool messages carry
    text only, so the images follow in a user message.
  - Keys come from the environment only. No test calls a real model: a scripted chat drives the live
    server, and the backends are checked against a fake SDK client and a mock server.
  - Cost: each view is a 1280 x 1024 image, so `--max-turns` (default 40) bounds a reading.
- **Correction to M5: edge cells of the coverage grid.**
  - Found by the serpentine agent on `cmu1_small`: 97.5% was the most any reading could reach.
  - A cell cut by the right or bottom slide border had its centre outside the slide, while views
    are clamped to the slide, so no view could ever see it.
  - Such cells are now centred on their part inside the slide, and tissue piece boxes are clipped
    to the slide.

## M6: evaluation and visualisation (2026-10-15)

- **Ground truth stays in `slidetwin/eval`.** It reads the manifest's `eval_only` blocks and the
  annotation files itself. A test checks that nothing in `core`, `api`, `mcp_server` or `agents`
  imports it.
- **Annotations.** ASAP XML becomes GeoJSON in slide micrometres, using the slide's own mpp (the
  frame the sessions use), in the git-ignored `data/annotations/`.
  - Groups: `Tumor`, `_0`, `_1` are tumour; `Exclusion`, `_2` are holes. Other groups are skipped.
  - The demo slides' annotations hold only `Tumor` polygons.
- **Deposits (draft definition, needs pathologist review).**
  - Tumour polygons are drawn on an 8 um grid, minus exclusions. Polygons whose cells touch
    (8-connected) form one deposit.
  - A deposit's size is its greatest dimension: the largest distance between two vertices of its
    polygons (the Feret diameter over their convex hull).
  - On the demo slides this gives 4.05 mm (`c16_fa751c`), 1.27 mm (`c16_aba3ae`), 0.28 mm
    (`c16_2abf4f`) and 4.74 mm (`c16_683d2d`), which agrees with the categories in the manifest.
- **Metrics of a reading** (`eval/metrics.py`).
  - Process: virtual time, views, coverage and violations from the receipt.
  - Outcome: conclusion against the label (`indeterminate` counts as not correct), and category.
  - Lesions: whether a view at 10x (screening) or 20x (confirmation) showed tumour, the tumour
    share seen at 10x, and the virtual time to the first such view.
  - Evidence: whether each finding's evidence views show tumour.
  - Size error: the reported size (the largest cited measurement, else `largest_deposit_mm`)
    against the largest deposit.
  - "Shows tumour" means at least one 8 um tumour cell has its centre inside the view.
- **Pictures** (`eval/visualize.py`). They go to `data/eval/` and stay out of git, because the
  CAMELYON licence terms are unclear.
  - A path overlay: every view as a rectangle coloured by magnification, joined in reading order.
  - A replay GIF (at most 80 frames, 640 px).
  - A coverage heatmap: tissue cells coloured by the highest magnification they were seen at.
  - Lesion outlines appear only in these pictures.
- **Profile comparison** (`eval/compare.py`). Each baseline runs under every profile it supports:
  serpentine and random under `viewer` and `unconstrained`, the crop agent under `unconstrained`
  only. Runs go through the REST API in process (an ASGI transport, no network).
- **Results.** In `docs/EVALUATION.md`: numbers and method only.
- **A finding from the first run, now fixed.** The serpentine agent made negative claims for three
  small tissue pieces that its own thumbnail mask had missed, and E4 rejected them. It now also
  scans the boxes of the tissue pieces the environment publishes, so every negative claim it makes
  rests on a full scan.

## M7: deployment and pilot (2026-10-15)

- **Slides on the Space: downloaded at start, not read remotely.**
  - Measured from the cluster for `tumor_093.tif` (one 1280 x 1024 view):
    - local disk: about 0.01 s;
    - tiffslide over HTTPS with fsspec: 1.2-2.3 s;
    - with fsspec's block cache: 0.2-0.8 s for the first read of a region.
  - Remote reads would make every move feel slow to a reader. The five CAMELYON demo slides are
    5.3 GB, which fits the Space's disk.
  - `python -m slidetwin.fetch_slides` (run by the image's entry point when
    `SLIDETWIN_FETCH_SLIDES=1`) downloads each manifest slide with a `source_url`, checks the pinned
    sha256 and renames the file only after the hash matches. It keeps files that are already
    present and correct, and skips slides without a URL.
  - A slide that fails to download is reported when it is opened; the server still starts.
- **Image.** `python -m slidetwin.serve` runs uvicorn with one worker (sessions live in memory)
  and trusts proxy headers (`FORWARDED_ALLOW_IPS`), so the rate limit sees the real client.
  - `/data/slides` belongs to the image's user, so downloads work without a volume. A volume
    mounted there (local slides) is used as it is.
  - The image could not be built here (no Docker on the cluster). Its parts (the fetcher, the entry
    point, the server) are tested separately.
- **Rate limit** (SPEC 14). A token bucket per client address on `/api` and `/mcp`, answered with
  `RATE_LIMITED` (429, `Retry-After`). It is off by default, so that local agents run at full
  speed. The image turns it on at 20 requests per second, with bursts of 60.
- **Restarts.** The start screen says that sessions live in memory and that the trajectory should
  be downloaded at the end.
- **Pilot.**
  - `docs/PILOT.md` is the study team's guide: setup, session plan, what to collect, questions,
    and calibration afterwards.
  - The participants' instructions are in the UI (Help), in English and Chinese.
  - An optional feedback form appears after the report (three 1-5 ratings and a comment). It is
    stored in `SLIDETWIN_LOG_DIR/feedback/<session>.json`, next to the log and never in it.
- **Calibration** (`scripts/calibrate_costs.py`, method in `eval/calibrate.py`).
  - The time between a person's actions, from the logged wall clock, is that action's cost:
    - medians for single-cost actions;
    - medians minus `view_render_s` for moves that serve a view;
    - a Theil-Sen line for pan (`pan_base_s` plus `pan_per_viewport_s` times the distance actually
      moved).
  - `view_render_s` is estimated only when there are at least 5 `view` actions; otherwise it keeps
    its current value, because it cannot be separated from the moves.
  - Breaks over 60 s and failed actions are left out. 95% intervals come from a bootstrap.
  - Agents' logs are ignored.
  - A simulation with known costs and 10% noise recovers them within 15%.
- **README animation.** Recorded from the UI on the CC0 test slide (`scripts/make_readme_gif.py`).
  The CAMELYON images are not used outside the running app.

