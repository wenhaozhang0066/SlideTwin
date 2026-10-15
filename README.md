---
title: SlideTwin
emoji: 🔬
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
short_description: Agent-operable digital pathology viewer simulator (research)
startup_duration_timeout: 1h
---

# SlideTwin

> **Research prototype. Not for clinical use or diagnosis.**

SlideTwin is a digital pathology slide viewer that AI agents and people operate under the same rules.
Every pixel shown in a session comes from a logged action (`view`, `thumbnail` or `crop`).
Positions and distances are in micrometres and time is a virtual clock in seconds, so a session can be
replayed exactly from its log.

It supports three kinds of experiments:

- **Faithfulness:** a conclusion may depend only on the fields of view that were logged.
- **Negative coverage:** a "no metastasis" claim must come with a proof of coverage.
- **Spatial measurement:** sizes and distances must be measured on the physical scale.

![A reading of the CC0 test slide in the web UI: overview, magnification keys, a measurement, a
polygon and the report receipt](docs/media/reading.gif)

中文说明见 [README_zh.md](README_zh.md).

## Status

| Milestone | Content | State |
|---|---|---|
| M0 | Scaffold, tooling, CI, health endpoint | done |
| M1 | Core engine: geometry, slide access, session, ledger, trajectory, replay | done |
| M2 | REST API for every action in SPEC section 7 | done |
| M5 | Rule checks E1-E4, tissue coverage, enforced and advisory protocols | done |
| M3 | Web reading UI (keyboard and pointer, English and Chinese) | done |
| M4 | MCP tools at `/mcp`; serpentine, random, crop and LLM agents | done |
| M6 | Lesion metrics, trajectory pictures, profile comparison, evaluation report | done |
| M7 | Docker image, slide download, rate limit, pilot guide and feedback, cost calibration | done; Space not yet published |

## Quick start

```bash
pip install -e ".[dev]"
python scripts/fetch_test_data.py           # 1.9 MB CC0 test slide
pytest -q
uvicorn slidetwin.api.main:app --reload --port 7860
```

Open http://localhost:7860/ to read a slide in the browser, or use the API below.

All checks, the same as CI: `bash scripts/ci_local.sh` (ruff, mypy, UI unit tests, pytest with the
coverage gate). The browser tests need `pip install -e ".[dev,e2e]"` and
`python -m playwright install chromium`; without them they are skipped.

## The API in one minute

Every action of a reading session is one HTTP call. Interactive docs are at `/docs`.

```bash
# open a slide (returns the first view, the slide geometry, the task and the rules)
curl -s -X POST localhost:7860/api/sessions -H 'content-type: application/json' \
  -d '{"slide_id": "cmu1_small", "actor": "agent:demo"}' > session.json
SID=$(python -c 'import json; print(json.load(open("session.json"))["session_id"])')

# move: every response carries cost_s, elapsed_s, the new state and the new view (base64 JPEG)
curl -s -X POST localhost:7860/api/sessions/$SID/magnification -H 'content-type: application/json' -d '{"mag": 10}'
curl -s -X POST localhost:7860/api/sessions/$SID/pan -H 'content-type: application/json' -d '{"dx": 0.5, "dy": 0}'
curl -s -X POST localhost:7860/api/sessions/$SID/measure -H 'content-type: application/json' \
  -d '{"p1_px": [100, 100], "p2_px": [400, 100]}'

# the log: one JSON line per action, replayable with slidetwin.core.replay
curl -s localhost:7860/api/sessions/$SID/trajectory > session.jsonl
```

| Action | Route |
|---|---|
| create_session | `POST /api/sessions` |
| get_state | `GET /api/sessions/{id}/state` |
| get_thumbnail | `POST /api/sessions/{id}/thumbnail` |
| set_magnification | `POST /api/sessions/{id}/magnification` |
| pan / move_to / view | `POST /api/sessions/{id}/pan`, `/move_to`, `/view` |
| measure / annotate | `POST /api/sessions/{id}/measure`, `/annotations` |
| crop (`unconstrained` only) | `POST /api/sessions/{id}/crop` |
| submit_report | `POST /api/sessions/{id}/report` |
| export_trajectory | `GET /api/sessions/{id}/trajectory` |
| report schema | `GET /api/report_schema` |

Settings come from environment variables: `SLIDETWIN_CONFIG_DIR`, `SLIDETWIN_SLIDE_DIR`,
`SLIDETWIN_LOG_DIR` (required; trajectories are written there as they happen), `SLIDETWIN_CACHE_DIR`,
`SLIDETWIN_MAX_SESSIONS`, `SLIDETWIN_SESSION_TTL_S`, `SLIDETWIN_SESSION_MAX_AGE_S`,
`SLIDETWIN_MAX_BODY_BYTES`, `SLIDETWIN_ALLOWED_HOSTS`, `SLIDETWIN_ALLOWED_ORIGINS`. The
default viewport is 1280 x 1024 px (see docs/DECISIONS.md).

## MCP and agents

Every session action is also an MCP tool at `/mcp` (streamable HTTP). Any MCP client can read
slides with it, Claude Code included:

```bash
claude mcp add --transport http slidetwin http://localhost:7860/mcp
```

Other clients have their own commands; see their documentation. On a public host name, set
`SLIDETWIN_ALLOWED_HOSTS` to that name (the default accepts localhost only).

Baseline agents run on your machine against a server, through REST or MCP:

```bash
python -m slidetwin.agents serpentine --env http://localhost:7860 --slide cmu1_small
python -m slidetwin.agents random --env http://localhost:7860 --transport mcp --slide cmu1_small
python -m slidetwin.agents crop --env http://localhost:7860 --slide cmu1_small   # unconstrained profile

# a vision LLM: the Anthropic API (pip install -e ".[llm]"; key in ANTHROPIC_API_KEY) ...
python -m slidetwin.agents llm --env http://localhost:7860 --slide cmu1_small --out reading.jsonl
# ... or any OpenAI-compatible endpoint, e.g. a local vLLM
python -m slidetwin.agents llm --backend openai --base-url http://localhost:8000/v1 \
  --model Qwen/Qwen2.5-VL-7B-Instruct --env http://localhost:7860 --slide cmu1_small
```

## Evaluation

Offline tools in `slidetwin/eval` score readings against the CAMELYON annotations, which are used
for evaluation only. The demo report is in [docs/EVALUATION.md](docs/EVALUATION.md).

```bash
python -m slidetwin.eval convert --xml-dir /path/to/CAMELYON16/annotations
python -m slidetwin.eval run --out data/eval/demo --slides c16_fa751c,c16_f606e8
python -m slidetwin.eval evaluate --out data/eval/demo    # metrics, report table, pictures
```

## Deployment

```bash
docker build -t slidetwin --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
docker run -p 7860:7860 slidetwin           # downloads the public demo slides (5.3 GB) at start
```

To read your own slides, mount them and a manifest, and skip the download:

```bash
docker run -p 7860:7860 -e SLIDETWIN_FETCH_SLIDES=0 \
  -v /path/to/slides:/data/slides \
  -v /path/to/manifest.yaml:/app/configs/slides/manifest.yaml slidetwin
```

The image limits each client to 20 requests per second (`SLIDETWIN_RATE_LIMIT_PER_S`). On a
Hugging Face Space, also set `SLIDETWIN_ALLOWED_HOSTS` to the Space's host name. Sessions live
in memory, so a restart ends them: download the trajectory at the end. If the download does not
start inside the Hugging Face page, open the Space in its own tab.

For a pathologist pilot, see [docs/PILOT.md](docs/PILOT.md). The cost model can be fitted from
human logs with `python scripts/calibrate_costs.py LOGS --fitted fitted.yaml`.

## Documentation

- Specification: [docs/SPEC.md](docs/SPEC.md) (Chinese)
- Architecture and contract: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Decision log: [docs/DECISIONS.md](docs/DECISIONS.md)
- Evaluation report: [docs/EVALUATION.md](docs/EVALUATION.md)
- Pilot guide: [docs/PILOT.md](docs/PILOT.md)
- Data sources and licenses: [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md)

## Data

No slides, patient data or company data are stored in this repository. Slides are read from
`SLIDETWIN_SLIDE_DIR` and listed in `configs/slides/manifest.yaml`. Each public slide's source and
license are recorded in [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md).

## License

Code: Apache-2.0 (see [LICENSE](LICENSE)). Data keep their own licenses.
