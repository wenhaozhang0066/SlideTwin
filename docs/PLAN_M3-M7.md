# SlideTwin: plan for M3-M7

Status: approved to run continuously (owner, 2026-10-15). The push to GitHub and the Hugging Face Space
happens once, at the end. Order of work: M5 first (the report receipt that the UI and the agents
show depends on it), then M3, M4, M6, M7. Commits stay labelled by milestone.

## M5: rules and report (SPEC 5.4)

Done 2026-10-15; see DECISIONS.md ("M5: rules and coverage").

- `core/coverage.py`:
  - Builds the tissue mask on the coverage grid (`protocol.coverage.grid_um`, default 32 um). It
    renders the slide at one pixel per cell, thresholds saturation with Otsu, and drops specks.
  - Tissue pieces are 8-connected components, numbered by descending area (piece 1 is the largest).
  - Coverage is the fraction of tissue cells inside views at or above `at_or_above_mag`.
  - The mask is cached per slide sha256 and grid.
- `get_thumbnail` also returns the tissue pieces (id, bbox, area), so a negative claim can say
  `tissue_piece:N`. They are derived from the slide at thumbnail scale and logged with the thumbnail.
- `core/rules.py` runs E1-E4, all parameters from the protocol YAML:
  - E1: cited view and measurement ids belong to the session.
  - E2: finding claims need a view at or above `confirm_min`.
  - E3: a measurement must span at least `min_pixels_across` view pixels, and the category must agree
    with the measured size under the category limits.
  - E4: negative claims (whole slide, a `tissue_piece:N` or a `region:x0,y0,x1,y1` scope) and
    `metastasis: absent` need the coverage requirement.
- Modes:
  - `advisory` accepts the report and records the violations (in `rule_violations` of the event).
  - `enforced` refuses with `RULE_VIOLATION` and keeps the session open, so the reader can look again.
- Tests: a passing and a failing case for every rule, and coverage on hand-made masks and views.

## M3: web UI (SPEC 10)

Done 2026-10-15; what was built and why is in DECISIONS.md ("M3: web UI").

- Vanilla ES-module JavaScript and Canvas in `slidetwin/web/`, served by FastAPI at `/`. It uses only the
  public REST API: no tile endpoint, no tile viewer.
- **Start screen.** Choose slide, profile and protocol, a pseudonym, and the timer visibility. The
  language follows the browser and can be switched (en/zh).
- **Reading screen.**
  - The view is shown at 1:1 device pixels; it scales down only if the window is smaller, and the scale
    is logged.
  - The minimap shows the thumbnail and the viewport box; clicking it calls `move_to`.
  - Magnification: buttons and keys 1-6. Pan: arrows or WASD (half a viewport); a drag sends one pan
    on release.
  - The scale bar is always visible. The measure tool takes two points; annotations are points or
    polygons with labels.
  - The report form is built from the protocol and the report schema. After submitting, the rule check
    results are shown.
  - The trajectory can be downloaded, and a "research use only" banner is always visible.
- **Design.** Neutral grey around the image and one accent colour (cyan) away from H&E pinks and
  purples. Everything works from the keyboard, with visible focus.
- **Tests.**
  - JS logic: node's built-in test runner.
  - Server: static routes, and a check that the JS calls only `/api` session routes.
  - A scripted browser run if a headless browser works on the cluster; otherwise an API-level run of
    the same request sequence, plus a manual check by the owner.

## M4: MCP and baseline agents (SPEC 12)

- **MCP server.** `slidetwin/mcp_server/` uses the official MCP SDK (FastMCP, streamable HTTP) and is
  mounted at `/mcp` in the same app. Its tools map 1:1 to the REST actions and return image content
  with text metadata, calling the same core methods.
- **Clients.** `slidetwin/agents/client.py` offers one interface over REST and over MCP.
- **Agents.**
  - `random_agent`.
  - `serpentine_agent`: 10x serpentine over tissue found in the thumbnail, then 20x checks of
    colour-flagged candidates, then a report based on coverage.
  - `unconstrained_crop_agent`: thumbnail plus crops, under the `unconstrained` profile only.
  - `llm_agent_example`: OpenAI-compatible, e.g. a local vLLM, or the Anthropic API, with keys from the
    environment only.
- **Tests.** The serpentine agent runs through REST and through MCP, and an MCP client completes a
  whole session.

## M6: evaluation and visualisation

- `scripts/convert_annotations.py` turns ASAP XML into micrometre GeoJSON. It is used for evaluation
  only; files go to the git-ignored `data/annotations/`.
- `slidetwin/eval/` provides:
  - a trajectory overlay on the thumbnail (PNG) with a replay GIF;
  - a coverage heatmap;
  - lesion metrics: whether evidence views hit lesions, and measurement error against the largest
    contiguous annotated deposit (touching polygons merged; the definition is a draft);
  - a profile comparison: the same agent under `viewer` and `unconstrained` gives one table.
- Acceptance: one evaluation report over the demo slides. Images stay out of git (license caution);
  the numbers and the method go to `docs/`.

## M7: deployment and pilot

- **Docker and the Space.**
  - Slides are fetched at startup from their public URLs into a cache, with progress. Remote reading
    through fsspec is measured from here and compared.
  - The Dockerfile cannot be built on this cluster; the Space builds it.
- **README.** An animated walkthrough made from a replay, or from browser screenshots if available.
- **Pilot pack.** Instructions for pathologists and a feedback form that saves a JSON file next to the
  trajectory.
- **Cost calibration.** `scripts/calibrate_costs.py` fits SPEC 8 from human trajectories: per-action
  wall times, with pan modelled as a base plus a per-viewport time. It is tested on synthetic logs.
- **Needs the owner.** The GitHub token, and the Space name and token, with permission to create the
  Space and to choose public or private.
