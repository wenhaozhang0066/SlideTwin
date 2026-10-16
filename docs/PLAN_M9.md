# SlideTwin: plan for M9 (pip package and training interface)

Status: draft, waiting for the owner's approval.
Owner's direction (2026-10-16): the final form is a pip-installable program. The owner trains
agents on the cluster with the cluster's slides, then open-sources the simulator (first) and the
agent. Others watch the demo in a browser (M10).

## Goals

1. `pip install slidetwin` gives a working program, with no repository checkout.
2. Agents in training call the environment in-process: no HTTP, no JPEG round trip, many
   environments in parallel, and the same rules and logs as the web UI and MCP.
3. The repository is ready to be made public.

## Package

- Configs (profiles, protocols, an example manifest) move into the package
  (`slidetwin/configs/`, read with `importlib.resources`). `SLIDETWIN_CONFIG_DIR` and a local
  `configs/` folder still take precedence, so the current layout keeps working.
- The demo manifest is separate from a user's manifest; nothing in the package names cluster
  paths.
- Extras: `openslide`, `slideio`, `isyntax`, `llm`, `e2e`, `dev`, and `all` (every reader).
- CI builds the wheel and the sdist, installs the wheel in a fresh environment and runs a smoke
  test from outside the repository (serve, open the CC0 slide, one view, MCP ping).
- The name `slidetwin` is free on PyPI (checked 2026-10-16). Publishing waits for the owner.

## Command line (`slidetwin`)

| Command | Does |
|---|---|
| `slidetwin serve [--slides DIR] [--manifest FILE] [--port N]` | web UI, REST API and MCP over HTTP (today's server) |
| `slidetwin mcp [--slides DIR] [--manifest FILE]` | MCP over stdio, for a local agent (e.g. `claude mcp add slidetwin -- slidetwin mcp`) |
| `slidetwin check PATH...` | format, reader, levels, mpp and file set of each slide; nothing is served |
| `slidetwin manifest DIR -o FILE` | a manifest for a folder of slides: opaque ids, hashes, formats; refused slides listed with the reason |
| `slidetwin replay LOG` | replays a trajectory and reports differences |
| `slidetwin fetch [--demo]` | downloads the demo or test slides |

## Training interface (`slidetwin.env`)

- `Env(manifest=..., slide_dir=..., profile="viewer", protocol="sln_metastasis", log_dir=None)`.
  - `reset(slide_id, *, seed=None, viewport=(1280, 1024)) -> Observation`
  - `step(action: dict) -> StepResult(observation, cost_s, elapsed_s, done, info)`, where the
    action is any session action in the same JSON as REST and MCP (`set_magnification`, `pan`,
    `move_to`, `view`, `crop`, `measure`, `annotate`, `submit_report`, ...).
  - Observations carry the view as a numpy RGB array (no JPEG encoding), the state and the rule
    violations; the receipt after `submit_report`.
- No reward is defined in the environment: the training code computes it, for example from the
  receipt or from `slidetwin.eval` metrics against ground truth. Ground truth never enters an
  observation (the same rule as today).
- `VecEnv(n, ...)`: n environments in worker processes, each with its own slide handles; `reset`
  and `step` take and return lists. Seeds make runs reproducible.
- Logging: with `log_dir`, every episode writes the same trajectory as a web session and can be
  replayed; without it, nothing is written (faster for training; evaluation runs set it).
- Speed target, measured and reported: actions per second for one environment and for 8 workers
  on a CAMELYON16 slide.

## Open-source readiness

- `CONTRIBUTING.md`, `SECURITY.md`, `CITATION.cff` and a changelog.
- A check that the repository and its history contain no internal paths, tokens or patient data.
- The licence of every bundled and optional dependency, in one table.

## Order of work

1. Packaged configs and the `slidetwin` command (serve, mcp, check, manifest, replay, fetch).
2. `Env` and `VecEnv` with tests (determinism, the same events as a REST session, worker
   failures) and the speed measurement.
3. Wheel build and smoke test in CI; docs (README sections for pip, the command line and the
   Python interface).
4. Open-source readiness files, then an adversarial review.

## Decisions for the owner

| # | Question | Recommended | Alternative |
|---|---|---|---|
| 1 | Publish to PyPI | at the open-source release, after your review | earlier, as a pre-release |
| 2 | Logs in training | off unless `log_dir` is set | always on (slower, more disk) |
| 3 | Observation image | numpy array by default, `image="jpeg"` to get the exact JPEG that REST and MCP serve (so an agent can train on what it will see later) | JPEG only |
