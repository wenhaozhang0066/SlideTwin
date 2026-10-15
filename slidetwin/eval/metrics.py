"""Metrics of one reading, computed from its trajectory and, for evaluation only, the ground truth.

- Process: virtual time, views, coverage and rule violations (from the receipt).
- Outcome: conclusion and category against the slide label.
- Lesions (annotated slides): whether a view at the screening or confirmation magnification ever
  showed tumour, the tumour share seen at the screening magnification, the virtual time to the first
  such view, whether each finding's evidence shows tumour, and the error of the reported size
  against the largest contiguous deposit. Definitions are drafts (DECISIONS.md, M6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from slidetwin.core.geometry import BBox
from slidetwin.eval.annotations import Lesions

SCREEN_MAG = 10.0  # defaults; a reading is judged by the protocol in its log header
CONFIRM_MAG = 20.0
_EPS = 1e-6
# The manifest may label a negative slide's category; a report says "none".
_NO_CATEGORY = {"negative", "normal", "none"}


def thresholds(header: Mapping[str, Any]) -> tuple[float, float]:
    """(screening, confirmation) magnifications of the protocol the reading was checked
    against: coverage counts at the first (E4), findings need the second (E2)."""
    content = (header.get("protocol") or {}).get("content") or {}
    screen = (content.get("coverage") or {}).get("at_or_above_mag", SCREEN_MAG)
    confirm = (content.get("magnification") or {}).get("confirm_min", CONFIRM_MAG)
    return float(screen), float(confirm)


def load_truth(manifest: Path) -> dict[str, dict[str, Any]]:
    """The `eval_only` block of every slide in the manifest (read here, never by the core)."""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    return {s["id"]: dict(s.get("eval_only") or {}) for s in data.get("slides", [])}


@dataclass(frozen=True)
class SeenView:
    seq: int
    view_id: str
    kind: str  # "view" or "crop"
    mag: float
    bbox: BBox
    t_s: float  # virtual time after the action


def views_of(events: list[dict[str, Any]]) -> list[SeenView]:
    out = []
    for e in events:
        obs = e.get("observation") or {}
        if obs.get("type") in {"view", "crop"}:
            out.append(
                SeenView(
                    e["seq"],
                    obs["view_id"],
                    obs["type"],
                    float(obs["mag"]),
                    BBox(*obs["bbox_um"]),
                    float(e["t_virtual_s"]),
                )
            )
    return out


def accepted_report(
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(report, receipt) of the accepted report, if any."""
    for e in reversed(events):
        if e["action"] == "submit_report" and not e.get("error"):
            return e["params"]["report"], e["observation"]
    return None, None


def measurements_of(events: list[dict[str, Any]]) -> dict[str, float]:
    """measurement_id -> length in um."""
    return {
        e["observation"]["measurement_id"]: float(e["observation"]["length_um"])
        for e in events
        if (e.get("observation") or {}).get("type") == "measurement"
    }


@dataclass
class ReadingMetrics:
    slide_id: str
    actor: str
    profile: str
    virtual_s: float
    views: int
    coverage: float | None
    violations: int | None
    metastasis: str | None
    category: str | None
    truth_label: str | None
    truth_category: str | None
    conclusion_correct: bool | None
    category_correct: bool | None
    lesion_seen_screen: bool | None
    lesion_seen_confirm: bool | None
    lesion_share_screen: float | None
    time_to_lesion_s: float | None
    findings: int
    findings_on_lesion: int | None
    reported_size_mm: float | None
    truth_size_mm: float | None
    size_error_mm: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _truth_metastasis(label: str | None) -> str | None:
    if label is None:
        return None
    return "present" if label.lower() in {"tumor", "tumour", "metastasis"} else "absent"


def lesion_share(lesions: Lesions, views: list[SeenView], min_mag: float) -> float:
    """Share of the tumour cells whose centre fell inside a view at `min_mag` or higher."""
    total = lesions.area_cells
    if total == 0:
        return 0.0
    seen = np.zeros(lesions.mask.shape, dtype=bool)
    for v in views:
        if v.mag >= min_mag * (1 - _EPS):
            rs, cs = lesions.slices(v.bbox)
            seen[rs, cs] = True
    return float((seen & lesions.mask).sum() / total)


def evaluate(
    header: dict[str, Any],
    events: list[dict[str, Any]],
    truth: dict[str, Any] | None = None,
    lesions: Lesions | None = None,
) -> ReadingMetrics:
    truth = truth or {}
    views = views_of(events)
    report, receipt = accepted_report(events)
    conclusion = (report or {}).get("conclusion") or {}
    label = truth.get("label")
    truth_category = truth.get("category")
    if isinstance(truth_category, str) and truth_category.lower() in _NO_CATEGORY:
        truth_category = "none"
    screen_mag, confirm_mag = thresholds(header)
    expected = _truth_metastasis(label)
    metastasis = conclusion.get("metastasis")
    category = conclusion.get("category")

    lengths = measurements_of(events)
    claims = (report or {}).get("claims") or []
    measured = [
        lengths[c["measurement_id"]] / 1000
        for c in claims
        if c.get("type") == "measurement" and c.get("measurement_id") in lengths
    ]
    # The size the reader states; failing that, the largest measurement the report cites.
    reported = conclusion.get("largest_deposit_mm")
    if reported is None and measured:
        reported = max(measured)

    by_id = {v.view_id: v for v in views}
    findings = [c for c in claims if c.get("type") == "finding"]
    lesion_metrics: dict[str, Any] = dict(
        lesion_seen_screen=None,
        lesion_seen_confirm=None,
        lesion_share_screen=None,
        time_to_lesion_s=None,
        findings_on_lesion=None,
        truth_size_mm=None,
        size_error_mm=None,
    )
    if lesions is not None and lesions.deposits:
        screen = [v for v in views if v.mag >= screen_mag * (1 - _EPS) and lesions.hits(v.bbox)]
        lesion_metrics.update(
            lesion_seen_screen=bool(screen),
            lesion_seen_confirm=any(v.mag >= confirm_mag * (1 - _EPS) for v in screen),
            lesion_share_screen=round(lesion_share(lesions, views, screen_mag), 4),
            time_to_lesion_s=min((v.t_s for v in screen), default=None),
            findings_on_lesion=sum(
                any(lesions.hits(by_id[i].bbox) for i in c.get("evidence") or [] if i in by_id)
                for c in findings
            ),
            truth_size_mm=lesions.largest_mm,
        )
        if reported is not None and lesions.largest_mm is not None:
            lesion_metrics["size_error_mm"] = round(abs(reported - lesions.largest_mm), 4)

    return ReadingMetrics(
        slide_id=header["slide"]["id"],
        actor=header["actor"],
        profile=header["profile"]["id"],
        virtual_s=float(events[-1]["t_virtual_s"]) if events else 0.0,
        views=len(views),
        coverage=None if receipt is None else receipt["coverage"]["fraction"],
        violations=None if receipt is None else receipt["violations"],
        metastasis=metastasis,
        category=category,
        truth_label=label,
        truth_category=truth_category,
        conclusion_correct=None if expected is None or report is None else metastasis == expected,
        category_correct=(
            None if truth_category is None or report is None else category == truth_category
        ),
        findings=len(findings),
        reported_size_mm=None if reported is None else round(float(reported), 4),
        **lesion_metrics,
    )
