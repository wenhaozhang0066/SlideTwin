"""Shared pieces of the baseline agents: results, report building, geometry of served views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from slidetwin.agents.client import EnvError, EnvSession, Step

TASK = "sln_metastasis"
BASELINE_NOTE = "Baseline agent with a colour heuristic; research use only, not a diagnosis."


@dataclass
class AgentResult:
    session_id: str
    receipt: dict[str, Any] | None  # the report receipt, or None when the report was refused
    trajectory: str
    notes: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float | None:
        if self.receipt is None:
            return None
        fraction: float = self.receipt["coverage"]["fraction"]
        return fraction


def report(
    metastasis: str,
    category: str,
    claims: list[dict[str, Any]],
    *,
    largest_mm: float | None = None,
    free_text: str = BASELINE_NOTE,
) -> dict[str, Any]:
    return {
        "task": TASK,
        "conclusion": {
            "metastasis": metastasis,
            "category": category,
            "largest_deposit_mm": largest_mm,
            "extranodal_extension": "not_assessable",
        },
        "claims": claims,
        "free_text": free_text,
    }


def view_point_to_um(obs: dict[str, Any], x_px: float, y_px: float) -> tuple[float, float]:
    """A pixel of a served view (or crop) in slide micrometres."""
    x0, y0, x1, y1 = obs["bbox_um"]
    w, h = obs["size_px"]
    return x0 + x_px * (x1 - x0) / w, y0 + y_px * (y1 - y0) / h


async def go_to(s: EnvSession, x_um: float, y_um: float, *, pan_limit: float = 1.01) -> Step:
    """Move the view centre to (x_um, y_um): a pan when the target is within about one viewport
    in each direction (as a reader steps along), otherwise a move_to (as an overview click)."""
    cx, cy = s.last.state["center_um"]
    fw, fh = s.last.state["fov_um"]
    dx, dy = (x_um - cx) / fw, (y_um - cy) / fh
    if abs(dx) <= pan_limit and abs(dy) <= pan_limit:
        return await s.pan(round(dx, 6), round(dy, 6))
    return await s.move_to(x_um, y_um)


async def finish(s: EnvSession, body: dict[str, Any], notes: list[str]) -> AgentResult:
    """Submit the report and export the log; a refusal (enforced protocol) is kept in the notes."""
    receipt: dict[str, Any] | None
    try:
        receipt = (await s.submit_report(body)).observation
    except EnvError as err:
        receipt = None
        notes.append(f"report refused: {err}")
    return AgentResult(s.session_id, receipt, await s.export_trajectory(), notes)
