# AGENTS.md

## Project
SlideTwin (working name): an agent-operable digital pathology viewer simulator.
Humans and AI agents read whole-slide images under the same rules, and every action
is logged in physical units (micrometers, seconds). Research prototype, not for clinical use.

- Full spec: @docs/SPEC.md
- Decision log (update every milestone): @docs/DECISIONS.md

## Non-negotiables
- One rule set for humans and agents. The web UI may only use the public Session API.
  No direct tile endpoints and no tile-loading viewers such as OpenSeadragon.
- Every pixel served in a session goes through view / thumbnail / crop and is logged.
- Coordinates and distances are in micrometers, origin at the level-0 top-left corner.
  Never guess mpp; refuse slides that lack it unless the manifest overrides it.
- Never upscale beyond the native scan resolution to fake a magnification.
- Pathology rules live in `configs/protocols/*.yaml` with `status: draft | confirmed`.
  Never hard-code them and never invent rules: mark uncertainties as draft and list them
  in DECISIONS.md as "needs pathologist review".
- Ground-truth annotations are for evaluation scripts only and must never be reachable
  through any session action.
- No patient or company data in the repo, the Hugging Face Space, tests, or screenshots.
  No secrets in git; read tokens from environment variables only.

## Commands
- Setup: `pip install -e ".[dev]"`
- Tests: `pytest -q`
- Lint and types: `ruff check . && mypy slidetwin`
- Run locally: `uvicorn slidetwin.api.main:app --reload --port 7860`

## Workflow
- Start each milestone in plan mode and wait for approval before writing code.
- Write tests first for geometry and coordinate code.
- At the end of each milestone: run all tests, update docs, commit with Conventional
  Commits, report what was done, and wait for review.
- Ask before creating remotes, pushing, or creating or updating the Hugging Face Space,
  and confirm whether the repository should be public or private.
