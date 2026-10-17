# Changelog

Versions follow semantic versioning from 1.0; until then, any release may change interfaces.

## 0.1.0 (2026-10-17)

- Core engine: geometry, slide access, sessions with virtual-time costs, a ledger, trajectories
  and bit-exact replay (M1).
- REST API for every session action (M2); web reading UI in English and Chinese (M3); MCP tools
  and baseline agents (M4); rule checks E1-E4 and tissue coverage (M5); lesion metrics, pictures
  and the evaluation report (M6); Docker image, slide download, rate limits and the pilot guide
  (M7).
- Every common whole-slide format, routed by content, with file sets and provenance (M8).
- A small demo: three public slides in 0.9 GB, for a codespace or the Docker image (M10).
- The `slidetwin` command, a pip-installable package with its configs, the training interface
  `slidetwin.env` (`Env`, `VecEnv`) and reader plugins (M9).
