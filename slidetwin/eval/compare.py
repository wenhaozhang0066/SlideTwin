"""Profile comparison and the evaluation report (SPEC 16, M6).

`run_matrix` runs agents on slides under each profile through the REST API (in process, no network)
and saves every trajectory. `evaluate_dir` scores the saved trajectories against the ground truth
and draws the pictures; `markdown` turns the scores into tables.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx

from slidetwin.agents import random_agent, serpentine_agent, unconstrained_crop_agent
from slidetwin.agents.client import Env
from slidetwin.agents.common import AgentResult
from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.config import ConfigStore
from slidetwin.core.coverage import tissue_map
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.trajectory import load_trajectory
from slidetwin.eval.annotations import build_lesions, load_regions
from slidetwin.eval.metrics import ReadingMetrics, evaluate, load_truth, views_of
from slidetwin.eval.visualize import Overview, coverage_heatmap, path_overlay, replay_gif

# Which profiles each baseline can run under: the crop agent needs the crop action.
AGENT_PROFILES = {
    "serpentine": ("viewer", "unconstrained"),
    "random": ("viewer", "unconstrained"),
    "crop": ("unconstrained",),
}


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    manifest: Path
    slide_dir: Path
    cache_dir: Path | None = None


def _manager(paths: Paths, log_dir: Path) -> SessionManager:
    registry = SlideRegistry.from_manifest(
        paths.manifest, paths.slide_dir, cache_dir=paths.cache_dir
    )
    return SessionManager(
        registry=registry, configs=ConfigStore(paths.config_dir), log_dir=log_dir, max_sessions=64
    )


async def _run_agent(env: Env, agent: str, slide: str, profile: str, seed: int) -> AgentResult:
    actor = f"agent:{agent}"
    if agent == "serpentine":
        return await serpentine_agent.run(env, slide, profile=profile, actor=actor)
    if agent == "random":
        return await random_agent.run(env, slide, profile=profile, seed=seed, actor=actor)
    if agent == "crop":
        return await unconstrained_crop_agent.run(env, slide, actor=actor)
    raise ValueError(f"unknown agent {agent}")


async def run_matrix(
    paths: Paths,
    out_dir: Path,
    slides: Sequence[str],
    agents: Sequence[str],
    *,
    seed: int = 0,
) -> list[Path]:
    """Run each agent on each slide under each of its profiles; returns the trajectory files."""
    traj_dir = out_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    manager = _manager(paths, out_dir / "server_logs")
    settings = Settings(
        config_dir=paths.config_dir,
        manifest=paths.manifest,
        slide_dir=paths.slide_dir,
        log_dir=out_dir / "server_logs",
        cache_dir=paths.cache_dir,
    )
    app = create_app(settings, manager=manager)
    written = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://slidetwin", timeout=None
        ) as http:
            env = Env.rest("http://slidetwin", client=http)
            for slide in slides:
                for agent in agents:
                    for profile in AGENT_PROFILES[agent]:
                        result = await _run_agent(env, agent, slide, profile, seed)
                        path = traj_dir / f"{slide}__{agent}__{profile}.jsonl"
                        path.write_text(result.trajectory, encoding="ascii")
                        written.append(path)
    finally:
        manager.close_all()
        manager.registry.close()
    return written


def evaluate_dir(
    paths: Paths,
    out_dir: Path,
    annotation_dir: Path | None,
    *,
    pictures: bool = True,
    gif_for: Sequence[str] = ("serpentine__viewer",),
) -> list[ReadingMetrics]:
    """Score every trajectory in `out_dir/trajectories`; pictures go to `out_dir/figures`."""
    truth = load_truth(paths.manifest)
    registry = SlideRegistry.from_manifest(
        paths.manifest, paths.slide_dir, cache_dir=paths.cache_dir
    )
    configs = ConfigStore(paths.config_dir)
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    results = []
    overviews: dict[str, Overview] = {}
    try:
        for path in sorted((out_dir / "trajectories").glob("*.jsonl")):
            header, events = load_trajectory(path)
            slide = header["slide"]["id"]
            geo = annotation_dir / f"{slide}.geojson" if annotation_dir else None
            regions = load_regions(geo) if geo is not None and geo.is_file() else []
            lesions = build_lesions(regions) if regions else None
            metrics = evaluate(header, events, truth.get(slide), lesions)
            results.append(metrics)
            if not pictures:
                continue
            source = registry.get(slide).source
            ov = overviews.setdefault(slide, Overview(source))
            views = views_of(events)
            stem = path.stem
            path_overlay(ov, views, regions).save(fig_dir / f"{stem}__path.png")
            protocol = configs.protocol(header["protocol"]["id"])
            tissue = tissue_map(source, protocol.coverage)
            coverage_heatmap(ov, tissue, views, regions).save(fig_dir / f"{stem}__coverage.png")
            if any(stem.endswith(tag) for tag in gif_for):
                small = Overview(source, long_side=640)
                replay_gif(small, views, fig_dir / f"{stem}__replay.gif", regions)
    finally:
        registry.close()
    (out_dir / "metrics.json").write_text(
        json.dumps([m.to_dict() for m in results], indent=1), encoding="utf-8"
    )
    return results


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


COLUMNS = [
    ("slide", "slide_id"),
    ("agent", "actor"),
    ("profile", "profile"),
    ("virtual s", "virtual_s"),
    ("views", "views"),
    ("coverage", "coverage"),
    ("violations", "violations"),
    ("conclusion", "metastasis"),
    ("truth", "truth_label"),
    ("category", "category"),
    ("lesion seen 10x/20x", None),
    ("lesion share 10x", "lesion_share_screen"),
    ("time to lesion s", "time_to_lesion_s"),
    ("findings on lesion", None),
    ("size error mm", "size_error_mm"),
]


def markdown(results: Sequence[ReadingMetrics]) -> str:
    """One row per reading."""
    head = "| " + " | ".join(name for name, _ in COLUMNS) + " |"
    rule = "|" + "|".join("---" for _ in COLUMNS) + "|"
    rows = [head, rule]
    for m in results:
        cells = []
        for name, key in COLUMNS:
            if name == "lesion seen 10x/20x":
                cells.append(f"{_fmt(m.lesion_seen_screen)}/{_fmt(m.lesion_seen_confirm)}")
            elif name == "findings on lesion":
                cells.append(f"{_fmt(m.findings_on_lesion)} of {m.findings}")
            elif key == "actor":
                cells.append(m.actor.removeprefix("agent:"))
            elif key in {"virtual_s", "time_to_lesion_s"}:
                cells.append(_fmt(getattr(m, key), 1))
            elif key == "coverage":
                cells.append("-" if m.coverage is None else f"{m.coverage:.1%}")
            elif key == "lesion_share_screen":
                v = m.lesion_share_screen
                cells.append("-" if v is None else f"{v:.0%}")
            else:
                cells.append(_fmt(getattr(m, str(key))))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def _mean(values: Sequence[float | None]) -> float | None:
    got = [v for v in values if v is not None]
    return sum(got) / len(got) if got else None


def _rate(values: Sequence[bool | None]) -> str:
    got = [v for v in values if v is not None]
    return f"{sum(got)}/{len(got)}" if got else "-"


def summary(results: Sequence[ReadingMetrics]) -> str:
    """One row per agent and profile: means over slides, and counts over the readings."""
    groups: dict[tuple[str, str], list[ReadingMetrics]] = {}
    for m in results:
        groups.setdefault((m.actor.removeprefix("agent:"), m.profile), []).append(m)
    rows = [
        "| agent | profile | readings | mean virtual s | mean views | mean coverage | violations "
        "| conclusion correct | lesion seen at 20x | findings on lesion | mean size error mm |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (agent, profile), ms in sorted(groups.items()):
        cov = _mean([m.coverage for m in ms])
        found = sum(m.findings_on_lesion or 0 for m in ms if m.findings_on_lesion is not None)
        findings = sum(m.findings for m in ms if m.findings_on_lesion is not None)
        cells = [
            agent,
            profile,
            str(len(ms)),
            _fmt(_mean([m.virtual_s for m in ms]), 1),
            _fmt(_mean([float(m.views) for m in ms]), 1),
            "-" if cov is None else f"{cov:.1%}",
            str(sum(m.violations or 0 for m in ms)),
            _rate([m.conclusion_correct for m in ms]),
            _rate([m.lesion_seen_confirm for m in ms]),
            f"{found}/{findings}" if findings else "-",
            _fmt(_mean([m.size_error_mm for m in ms])),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"
