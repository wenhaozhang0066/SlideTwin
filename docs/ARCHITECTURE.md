# Architecture and core contract

This is the binding contract for `slidetwin.core` (M1) and the REST layer (M2). If code and this document
disagree, one of them is a bug. SPEC sections are cited as "SPEC n".

## Layers

```
api/ (FastAPI)   mcp_server/ (M4)   web/ (M3)
        \             |              /
         SessionManager (core/manager.py)      <- the only entry point for adapters
                 |
              Session (core/session.py)        <- state machine, one per reading session
        /        |          |          \
  Ledger     render.py   ImageStore   TrajectoryWriter
 (costs)   (ViewerBackend)              (JSONL log)
                 |
        SlideSource (tiffslide | openslide | array)
```

`core` imports nothing from FastAPI, MCP or the web UI. Adapters call only `SessionManager` and the
`Session` methods listed below. There is no tile endpoint and no way to read pixels outside a session
action.

## Units and coordinates (SPEC 3)

- **Slide frame.** Micrometres (um), origin at the top-left corner of level 0, x to the right and y down.
  A `BBox(x0, y0, x1, y1)` is half-open: `[x0, x1) x [y0, y1)`.
- **Level-0 pixels.** `x_px = x_um / mpp_x` and `y_px = y_um / mpp_y`, using the slide's own mpp per axis.
- **Level L.** It covers the whole slide. Its per-axis downsample is `w0 / wL` and `h0 / hL`, so its mpp is
  `(mpp_x * w0 / wL, mpp_y * h0 / hL)`.
- **View pixels.** A view has square pixels of size `mpp`. Pixel `(u, v)` covers the rectangle starting at
  `(x0 + u*mpp, y0 + v*mpp)`. Conversions are `view_px_to_um` and `um_to_view_px`.
- **Magnification.** `mpp(m) = mpp_ref_40x * 40 / m` (default `mpp_ref_40x = 0.25`).
  - A preset m is available iff `mpp(m) >= native_mpp * (1 - tolerance)`, where `native_mpp = max(mpp_x, mpp_y)`.
  - A request for an unavailable preset fails with `MAG_UNAVAILABLE`.
  - A value that is not a preset fails with `MAG_NOT_PRESET`.
- **Time.** The virtual clock is integer microseconds inside the ledger and is reported as seconds.
  Wall-clock time is recorded separately (`t_wall_ms`) and never affects the virtual clock.

## Rendering (`render.py`, SPEC 3 "reading implementation")

`render_region(source, bbox, out_size, fill_rgb) -> uint8[h, w, 3]` is deterministic.

1. **Target resolution.** Per axis, `t = bbox size / out_size`.
2. **Level choice.** `choose_level` picks the coarsest level whose mpp is `<= t` on both axes (relative
   epsilon 1e-9). If no level qualifies, which happens only within the magnification tolerance, it uses
   the finest level.
3. **Pre-reduction.** If the chosen level is still at least 2x finer than the target (missing pyramid
   levels), it first reduces by an integer box factor `k = floor(min scale)`.
4. **Resampling.** It reads an integer-aligned window around the exact fractional source box, padded for
   the filter support, and resamples it with Pillow `resize(..., LANCZOS, box=fractional box)`. Placement
   is exact to the sub-pixel.
5. **Outside the slide.** Pixels outside the slide are `fill_rgb`. This happens only when the field of view
   is larger than the slide.

Upsampling beyond native resolution happens only within the magnification tolerance. Crops never upsample.

## Viewer semantics (SPEC 4, 7, 8)

- **Creating a session.**
  - The session starts at the slide centre at the lowest available preset.
  - The initial view `v_0001` costs `view_render_s` (`viewer`) or 0 (`flat`).
  - It also returns the slide info (size in px and um, mpp, available magnifications) and the task
    description: the full protocol, with its rules, thresholds, coverage requirement, hints, report
    fields and `status`.
- **Clamping (D3).**
  - The centre is clamped so that the field of view stays inside the slide.
  - On an axis where the field of view is wider than the slide, the view is centred on that axis.
- **`pan(dx, dy)`** moves by `dx` viewport widths and `dy` viewport heights. Cost under `viewer`:
  `pan_base_s + pan_per_viewport_s * d + view_render_s`, where `d = hypot(Δx / fov_w, Δy / fov_h)` is
  the **actual** displacement after clamping.
- **`move_to(x_um, y_um)`** costs `move_to_s + view_render_s` and is clamped.
- **`set_magnification(m)`** costs `set_magnification_s + view_render_s` and keeps the centre, then
  re-clamps.
- **`view()`** costs `view_render_s` and serves a new view of the current state.
- **`auto_view: false`.** Moves do not render and `view_render_s` is not charged. `current_view_id` becomes
  null until `view()`.
- **`get_thumbnail()`** renders the whole slide with a long side of at most `thumbnail.long_side_px`,
  never finer than the scan (a small slide gets a smaller thumbnail), and reports `um_per_px` per
  axis.
  - Cost is `thumbnail_s`; it is free under `unconstrained`.
  - It fails with `ACTION_NOT_ALLOWED` if the profile forbids thumbnails.
  - The thumbnail does not change the current view.
- **`crop(x_um, y_um, w_um, h_um)`** is available only in profiles with `crop.allowed`.
  - The rectangle is clamped to the slide.
  - Output mpp is `max(max(w, h) / viewport_long_side, native_mpp)`. The output size is rounded and the
    returned bbox is adjusted, by less than one pixel, to keep pixels square.
  - The crop becomes the current view (`kind: "crop"`); the centre and magnification state are unchanged.
- **`measure(p1_px, p2_px)`** takes points in the current view's pixels, from `[0, w] x [0, h]`
  inclusive.
  - It returns `length_um`, `length_px` and both endpoints in um.
  - Cost is `measure_s`.
  - It fails with `NO_CURRENT_VIEW` or `OUTSIDE_VIEW` when the view or points are invalid.
- **`annotate(geometry, label, note)`** takes a point (`[x, y]`) or a polygon (3 or more `[x, y]`) in
  current-view pixels.
  - It is stored in pixels and in um.
  - Cost is `annotate_s`.
- **`submit_report(report)`** validates the report against `report_schema.json`, closes the session and
  returns a receipt.
  - The receipt lists the views used. Its `coverage` and `rule_checks` are filled in from M5.
  - The report models are strict: booleans and numeric strings are not numbers, and evidence must be a
    list of ids.
- **`get_state()` and `export_trajectory()`** cost 0 and are still allowed after the session closes.
- **Flat cost model (`unconstrained`).**
  - Every action other than get_state, export_trajectory and submit_report costs `step_s`.
  - Thumbnails cost `thumbnail_s` (0 by default).
- **Budget.**
  - Time: once `elapsed >= max_virtual_s`, every action except submit_report, get_state and
    export_trajectory fails with `BUDGET_EXHAUSTED`.
  - Views: once `views_served >= max_views`, only actions that would serve another view fail (view,
    crop, and moves when `auto_view` is on). The last view can still be measured and annotated, and the
    thumbnail is not a view.
- **Two phases per action.**
  - Plan: validate and compute without touching state. A failure is logged at zero cost and changes
    nothing.
  - Commit: charge, store the image, apply the state and append the event. Any failure rolls every
    change back (no id is used up) and is logged as `INTERNAL`.
  - A session that cannot even log a failure stops: every later action fails with `INTERNAL`, so no
    pixel is ever served unlogged.
- **`close()`** ends a session for good: it waits for an action in progress, then every later action
  and image request fails with `SESSION_CLOSED`.
- **Ids.**
  - Views and crops: `v_0001`...
  - Thumbnails: `t_0001`...
  - Measurements: `m_0001`...
  - Annotations: `a_0001`...
- **Actor.** Either `human:<alias>` or `agent:<name>`. The part after the colon is 1 to 64 characters from
  `[A-Za-z0-9._-]` (exact match, ASCII only).
- **Client metadata** (`client`, at most 4 KB of JSON) never decides an action: invalid or oversized
  metadata is replaced by `{"rejected": "<reason>"}` and the action proceeds.

## Profile `microscope` (stub, SPEC 4.3; not implemented)

This profile is reserved and specified here only. It has no YAML file yet, because its cost model and
actions do not exist. TODO when it is scheduled:

- **No thumbnail.** It offers only a naked-eye tissue outline (`get_overview`): a low-resolution tissue
  silhouette without stain detail, derived from the tissue mask.
- **Discrete objectives.** The preset list becomes the objective set. The field of view is circular,
  `FN / objective` mm, with the eyepiece field number in the profile.
- **Stage timing.**
  - A new cost kind `stage`: travel time proportional to distance (`stage_speed_mm_s`) plus
    `objective_change_s`.
  - `move_to` is unavailable (there is no minimap to click); navigation is by stage moves only.
- Non-goals stay: no focus, z-stack or optical degradation (SPEC 2).

## Errors

Every failure is a `SlideTwinError` with `code`, `message` and an optional `hint`. Messages say what is
wrong and how to fix it, name slides only by their public id, and echo inputs only in shortened form.
A failed session action also carries `seq`, `cost_s` (0) and `elapsed_s`, so a client can match it to
its trajectory line. The codes are:

- `INVALID_PARAMETER`
- `MAG_NOT_PRESET`, `MAG_UNAVAILABLE`
- `OUTSIDE_VIEW`, `NO_CURRENT_VIEW`
- `ACTION_NOT_ALLOWED`, `BUDGET_EXHAUSTED`
- `SESSION_CLOSED`, `SESSION_NOT_FOUND`, `TOO_MANY_SESSIONS`
- `IMAGE_NOT_FOUND`
- `SLIDE_NOT_FOUND`, `SLIDE_METADATA`, `SLIDE_HASH_MISMATCH`
- `SLIDE_READ_ERROR` (a tile could not be decoded)
- `CONFIG_ERROR`, `REPORT_INVALID`, `REPLAY_ERROR`
- `INTERNAL` (an unexpected failure; the action was not applied)

## Images and hashes

- **Encoding.**
  - JPEG with the profile's quality (default 90) and subsampling (default 4:4:4).
  - No metadata, and no progressive or optimised encoding.
- **Hashes.**
  - `pixel_sha256` is the sha256 of `"{h}x{w}x3:"` followed by the raw RGB bytes.
  - `jpeg_sha256` is the sha256 of the encoded file.
- **Storage.** Images live in an `ImageStore` and can be fetched only by the id of an image already served
  in that session.

## Trajectory (SPEC 9)

JSONL. The first line is the header; every action, including failed ones, adds one event line, flushed
immediately. Lines are pure ASCII JSON (non-ASCII text is `\u`-escaped) separated by `\n` only, so no
text a reader types can split or corrupt a line. A line that cannot be written completely is cut back.

- **Header** (`type: "header"`) contains:
  - `schema_version`, `app {version, git_sha}`, `session_id`, `created_utc`.
  - `slide {id, sha256, mpp, dimensions_px, backend}`.
  - `profile {id, sha256, content}` and `protocol {id, sha256, status, content}`, where `content` is the
    full normalised config, so the log is self-contained.
  - `viewport_px` and `actor`.
- **Event** (`type: "event"`) contains `seq`, `action`, `params`, `t_virtual_s`, `t_wall_ms`, `cost_s`,
  `state_after {center_um, mag, bbox_um}`, `view_id`, `observation`, `rule_violations`, `error` and
  `client`.
  - `client` is optional metadata reported by the client, such as `think_ms`. It never affects the clock.
  - `params` of a successful event are canonical: the values the action actually used (floats, lists,
    the validated report), so they always replay. A failed event logs a JSON-safe copy of the raw input,
    replaced by `{"truncated", "bytes", "sha256"}` above 16 KB.
- **Replay.**
  - `replay(log, registry)` re-runs every action on a fresh session.
  - It compares error codes, `cost_s`, `t_virtual_s`, `state_after`, `view_id` and every observation
    (pixel hashes included; not the JPEG hash or the observation's own seq).
  - It skips `INTERNAL` events: they record environment failures (for example a full disk) that applied
    nothing and cannot be re-run. The report counts them as `skipped`.
  - It requires the same slide sha256 and the same profile and protocol hashes, and turns any
    structural damage in the log into `REPLAY_ERROR`.

## Rules and coverage (M5)

- **`core/coverage.py`.**
  - Builds the tissue mask and pieces on the coverage grid from the slide's own pixels.
  - Marks cells seen by views at or above the protocol magnification, using the cell centre.
  - Reports coverage for the whole slide, a piece or a region.
- **`core/rules.py`.** Runs E1-E4 on the validated report with facts from the session (views,
  measurements, tissue). It only interprets thresholds taken from the protocol.
- **`submit_report`.** The receipt holds `rule_checks`, `violations` and `coverage`.
  - Under an enforced protocol, failures refuse the report with `RULE_VIOLATION`.
  - Failed checks always go to the event's `rule_violations`.
- **`get_thumbnail`.** Its observation lists the tissue pieces.

## REST layer (M2)

`slidetwin/api` is a thin adapter: request models (`schemas.py`), one route per action (`routes.py`),
error mapping and the body-size limit (`errors.py`), settings from the environment (`settings.py`).
Every payload it returns is built by the core (`Session.opening()`, `SessionManager.catalogue()`,
`.profiles()`, `.protocols()`), so the MCP adapter (M4) serves the same. Request numbers are strict, as
in the core. OpenAPI is served at `/openapi.json` and `/docs`, with the report schema inlined; the
schema alone is at `GET /api/report_schema`. The route table and status codes are in DECISIONS.md.

Sessions end after 2 h without an action or 8 h in total. Reading the state, the images or the
trajectory is not activity. When a session ends its images are deleted; its trajectory stays
downloadable.

## Ground truth isolation

The slide manifest may hold an `eval_only` block for evaluation scripts. The core loader deletes it before
validation. No core object, API response or log ever contains it. Associated images (label, macro,
thumbnail) are never exposed.

Slide ids are what every reader sees, so they are opaque (CAMELYON ids are `c16_` plus the first six hex
digits of the file's sha256). The loader refuses an entry whose id, license or attribution contains any
`eval_only` value. File paths and source URLs stay on the server; errors name the slide id only.

Limitation: a public slide can still be identified from its pixel dimensions or file hash, which the
session and its log reveal. Experiments that need blind readers should run agents without internet
access or use non-public slides.
