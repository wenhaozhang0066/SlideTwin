"""Cost model and virtual clock (SPEC 8).

Virtual time depends only on the action sequence. It is kept in integer microseconds so that a
replay reproduces it bit for bit.
"""

from __future__ import annotations

from slidetwin.core.config import Budget, CostModel, ViewerCostModel
from slidetwin.core.errors import BudgetExhausted

# Allowed even when the budget is used up (SPEC 7).
BUDGET_EXEMPT = frozenset({"get_state", "export_trajectory", "submit_report"})
ACTIONS = frozenset(
    {
        "create_session",
        "get_state",
        "get_thumbnail",
        "set_magnification",
        "pan",
        "move_to",
        "view",
        "measure",
        "annotate",
        "crop",
        "submit_report",
        "export_trajectory",
    }
)


def to_us(seconds: float) -> int:
    return round(seconds * 1_000_000)


def to_s(us: int) -> float:
    return us / 1_000_000


class Ledger:
    def __init__(self, cost_model: CostModel, budget: Budget, *, auto_view: bool) -> None:
        self.cost_model = cost_model
        self.budget = budget
        self.auto_view = auto_view
        self.elapsed_us = 0
        self.views_served = 0

    def cost_us(self, action: str, *, viewports: float = 0.0) -> int:
        """Virtual cost of a successful action.

        `viewports` is the pan distance in viewport units (x in widths, y in heights).
        """
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        if action in BUDGET_EXEMPT:
            return 0
        cm = self.cost_model
        if not isinstance(cm, ViewerCostModel):
            if action == "create_session":
                return 0
            if action == "get_thumbnail":
                return to_us(cm.thumbnail_s)
            return to_us(cm.step_s)
        render = to_us(cm.view_render_s)
        moved_render = render if self.auto_view else 0
        if action == "create_session" or action in ("view", "crop"):
            return render
        if action == "set_magnification":
            return to_us(cm.set_magnification_s) + moved_render
        if action == "pan":
            return (
                to_us(cm.pan_base_s)
                + round(to_us(cm.pan_per_viewport_s) * viewports)
                + moved_render
            )
        if action == "move_to":
            return to_us(cm.move_to_s) + moved_render
        if action == "get_thumbnail":
            return to_us(cm.thumbnail_s)
        if action == "measure":
            return to_us(cm.measure_s)
        return to_us(cm.annotate_s)  # annotate

    def check(self, action: str) -> None:
        """Raise if the budget is used up and the action is not exempt."""
        if action in BUDGET_EXEMPT:
            return
        b = self.budget
        if b.max_virtual_s is not None and self.elapsed_us >= to_us(b.max_virtual_s):
            raise BudgetExhausted(
                f"The time budget is used up ({to_s(self.elapsed_us):g} s of "
                f"{b.max_virtual_s:g} s).",
                hint="Only submit_report, get_state and export_trajectory are allowed now.",
            )
        if b.max_views is not None and self.views_served >= b.max_views:
            raise BudgetExhausted(
                f"The view budget is used up ({self.views_served} of {b.max_views} views).",
                hint="Only submit_report, get_state and export_trajectory are allowed now.",
            )

    def charge(self, cost_us: int) -> None:
        self.elapsed_us += cost_us

    def note_view(self) -> None:
        self.views_served += 1

    @property
    def elapsed_s(self) -> float:
        return to_s(self.elapsed_us)

    def remaining(self) -> dict[str, float | int | None]:
        b = self.budget
        return {
            "virtual_s": None
            if b.max_virtual_s is None
            else max(0.0, to_s(to_us(b.max_virtual_s) - self.elapsed_us)),
            "views": None if b.max_views is None else max(0, b.max_views - self.views_served),
        }
