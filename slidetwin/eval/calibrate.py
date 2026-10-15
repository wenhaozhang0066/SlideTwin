"""Fit the viewer cost model (SPEC 8) from human reading logs (M7).

The virtual clock charges each action a fixed cost. In a human session the log also holds the wall
clock, so the time between two actions is what that action took a person: deciding, acting and
looking at the result. Per action type:

- thumbnail, measure, annotate: the median interval;
- set_magnification, move_to: the median interval minus `view_render_s` (each serves a view);
- pan: a Theil-Sen line through (distance moved in viewports, interval - view_render_s), giving
  `pan_base_s` (intercept) and `pan_per_viewport_s` (slope);
- view_render_s: the median interval of `view` actions when there are enough, else kept as given.

Intervals longer than `max_gap_s` (breaks) and failed actions are left out. 95% intervals come from
a bootstrap over the actions. A parameter without data keeps its current value.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from statistics import median
from typing import Any

FITTED = ("get_thumbnail", "set_magnification", "pan", "move_to", "view", "measure", "annotate")
MIN_VIEWS = 5  # `view` actions needed to estimate view_render_s itself


@dataclass(frozen=True)
class Interval:
    action: str
    seconds: float
    viewports: float = 0.0  # pan distance actually moved


def intervals(
    header: Mapping[str, Any], events: list[dict[str, Any]], *, max_gap_s: float = 60.0
) -> list[Interval]:
    """The intervals of one human session (none for agents, whose wall clock is not a person's)."""
    if not str(header.get("actor", "")).startswith("human:") or not events:
        return []
    out = []
    prev_t = events[0]["t_wall_ms"]
    prev_state = events[0]["state_after"]
    for e in events[1:]:
        dt = (e["t_wall_ms"] - prev_t) / 1000
        prev_t = e["t_wall_ms"]
        if e.get("error"):
            continue
        state = e["state_after"]
        if e["action"] in FITTED and 0 <= dt <= max_gap_s:
            moved = 0.0
            if e["action"] == "pan":
                x0, y0, x1, y1 = state["bbox_um"]
                (ax, ay), (bx, by) = prev_state["center_um"], state["center_um"]
                moved = math.hypot((bx - ax) / (x1 - x0), (by - ay) / (y1 - y0))
            out.append(Interval(e["action"], dt, moved))
        prev_state = state
    return out


def theil_sen(xs: list[float], ys: list[float], *, max_pairs: int = 20000) -> tuple[float, float]:
    """(intercept, slope): the median pairwise slope and the median residual intercept."""
    pairs = [(i, j) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    if len(pairs) > max_pairs:
        pairs = random.Random(0).sample(pairs, max_pairs)
    if not pairs:
        return median(ys), 0.0
    slope = median((ys[j] - ys[i]) / (xs[j] - xs[i]) for i, j in pairs)
    return median(y - slope * x for x, y in zip(xs, ys, strict=True)), slope


def _estimate(data: list[Interval], current: Mapping[str, float]) -> dict[str, float]:
    by: dict[str, list[Interval]] = {}
    for iv in data:
        by.setdefault(iv.action, []).append(iv)
    out = dict(current)
    views = by.get("view", [])
    render = (
        median(v.seconds for v in views) if len(views) >= MIN_VIEWS else current["view_render_s"]
    )
    out["view_render_s"] = render
    for action, key in [
        ("get_thumbnail", "thumbnail_s"),
        ("measure", "measure_s"),
        ("annotate", "annotate_s"),
    ]:
        if by.get(action):
            out[key] = median(v.seconds for v in by[action])
    for action, key in [("set_magnification", "set_magnification_s"), ("move_to", "move_to_s")]:
        if by.get(action):
            out[key] = max(0.0, median(v.seconds for v in by[action]) - render)
    pans = by.get("pan", [])
    if pans:
        base, slope = theil_sen([p.viewports for p in pans], [p.seconds - render for p in pans])
        out["pan_base_s"], out["pan_per_viewport_s"] = max(0.0, base), max(0.0, slope)
    return out


@dataclass
class Fit:
    params: dict[str, float]
    current: dict[str, float]
    counts: dict[str, int]
    sessions: int
    ci95: dict[str, tuple[float, float]] = field(default_factory=dict)

    def yaml(self) -> str:
        lines = [
            f"# Fitted from {self.sessions} human session(s), {sum(self.counts.values())} actions,",
            "# by scripts/calibrate_costs.py. Review before use; see docs/DECISIONS.md (M7).",
            "cost_model:",
            "  kind: viewer",
        ]
        lines += [f"  {k}: {round(v, 3)}" for k, v in self.params.items()]
        return "\n".join(lines) + "\n"

    def markdown(self) -> str:
        source = {
            "view_render_s": "view",
            "set_magnification_s": "set_magnification",
            "pan_base_s": "pan",
            "pan_per_viewport_s": "pan",
            "move_to_s": "move_to",
            "thumbnail_s": "get_thumbnail",
            "measure_s": "measure",
            "annotate_s": "annotate",
        }
        rows = ["| parameter | n | fitted | 95% interval | current |", "|---|---|---|---|---|"]
        for key, value in self.params.items():
            n = self.counts.get(source[key], 0)
            lo, hi = self.ci95.get(key, (math.nan, math.nan))
            ci = "-" if math.isnan(lo) else f"{lo:.2f}-{hi:.2f}"
            rows.append(f"| {key} | {n} | {value:.2f} | {ci} | {self.current[key]:.2f} |")
        return "\n".join(rows) + "\n"


def fit(
    sessions: Iterable[tuple[Mapping[str, Any], list[dict[str, Any]]]],
    current: Mapping[str, float],
    *,
    max_gap_s: float = 60.0,
    n_boot: int = 300,
    seed: int = 0,
) -> Fit:
    """Fit the cost model from (header, events) pairs; `current` is the profile's cost model."""
    base = {k: float(v) for k, v in current.items() if k != "kind"}
    data: list[Interval] = []
    n_sessions = 0
    for header, events in sessions:
        got = intervals(header, events, max_gap_s=max_gap_s)
        n_sessions += bool(got)
        data += got
    counts: dict[str, int] = {}
    for iv in data:
        counts[iv.action] = counts.get(iv.action, 0) + 1
    result = Fit(_estimate(data, base), base, counts, n_sessions)
    if data and n_boot > 0:
        rng = random.Random(seed)
        draws: dict[str, list[float]] = {k: [] for k in result.params}
        for _ in range(n_boot):
            sample = [data[rng.randrange(len(data))] for _ in data]
            for k, v in _estimate(sample, base).items():
                draws[k].append(v)
        for k, values in draws.items():
            values.sort()
            result.ci95[k] = (
                values[int(0.025 * (len(values) - 1))],
                values[math.ceil(0.975 * (len(values) - 1))],
            )
    return result
