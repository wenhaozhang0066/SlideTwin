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
| M2 | REST API for every action in SPEC section 7 | planned |
| M3-M7 | Web UI, MCP and baseline agents, rule checks, evaluation, deployment | later |

## Quick start

```bash
pip install -e ".[dev]"
python scripts/fetch_test_data.py           # 1.9 MB CC0 test slide
pytest -q
uvicorn slidetwin.api.main:app --reload --port 7860
```

All checks, the same as CI: `bash scripts/ci_local.sh` (ruff, mypy, pytest with the coverage gate).

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
