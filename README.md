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

中文说明见 [README_zh.md](README_zh.md).

## Status

| Milestone | Content | State |
|---|---|---|
| M0 | Scaffold, tooling, CI, health endpoint | done |
| M1 | Core engine: geometry, slide access, session, ledger, trajectory, replay | done |
| M2 | REST API for every action in SPEC section 7 | done |
| M5 | Rule checks E1-E4, tissue coverage, enforced and advisory protocols | done |
| M3, M4, M6, M7 | Web UI, MCP and baseline agents, evaluation, deployment | in progress |

## Quick start

```bash
pip install -e ".[dev]"
python scripts/fetch_test_data.py           # 1.9 MB CC0 test slide
pytest -q
uvicorn slidetwin.api.main:app --reload --port 7860
```

All checks, the same as CI: `bash scripts/ci_local.sh` (ruff, mypy, pytest with the coverage gate).

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
`SLIDETWIN_MAX_BODY_BYTES`. The default viewport is 1280 x 1024 px (see docs/DECISIONS.md).

## Documentation

- Specification: [docs/SPEC.md](docs/SPEC.md) (Chinese)
- Decision log: [docs/DECISIONS.md](docs/DECISIONS.md)
- Data sources and licenses: [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md)

## Data

No slides, patient data or company data are stored in this repository. Slides are read from
`SLIDETWIN_SLIDE_DIR` and listed in `configs/slides/manifest.yaml`. Each public slide's source and
license are recorded in [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md).

## License

Code: Apache-2.0 (see [LICENSE](LICENSE)). Data keep their own licenses.
