from __future__ import annotations

import pytest

from slidetwin.core.config import Budget, FlatCostModel, Profile
from slidetwin.core.errors import BudgetExhausted
from slidetwin.core.ledger import ACTIONS, BUDGET_EXEMPT, Ledger, to_s, to_us


def viewer_ledger(viewer: Profile, auto_view: bool = True, **budget: float | int) -> Ledger:
    return Ledger(viewer.cost_model, Budget.model_validate(budget), auto_view=auto_view)


def test_appendix_b_pan_cost(viewer: Profile) -> None:
    # SPEC appendix B: pan dx=0.5 costs 0.575 s
    assert viewer_ledger(viewer).cost_us("pan", viewports=0.5) == 575_000


@pytest.mark.parametrize(
    ("action", "auto", "expected"),
    [
        ("create_session", True, 0.3),
        ("view", True, 0.3),
        ("crop", True, 0.3),
        ("set_magnification", True, 0.8),
        ("set_magnification", False, 0.5),
        ("move_to", True, 0.9),
        ("move_to", False, 0.6),
        ("pan", False, 0.2),
        ("get_thumbnail", True, 0.5),
        ("measure", True, 3.0),
        ("annotate", True, 4.0),
        ("get_state", True, 0.0),
        ("export_trajectory", True, 0.0),
        ("submit_report", True, 0.0),
    ],
)
def test_viewer_cost_table(viewer: Profile, action: str, auto: bool, expected: float) -> None:
    assert viewer_ledger(viewer, auto_view=auto).cost_us(action) == to_us(expected)


def test_flat_cost_table() -> None:
    led = Ledger(FlatCostModel(step_s=1.0), Budget(), auto_view=True)
    for action in ACTIONS:
        cost = led.cost_us(action, viewports=3.0)
        if action in BUDGET_EXEMPT or action in ("create_session", "get_thumbnail"):
            assert cost == 0, action
        else:
            assert cost == 1_000_000, action
    assert (
        Ledger(FlatCostModel(step_s=1.0, thumbnail_s=0.25), Budget(), auto_view=True).cost_us(
            "get_thumbnail"
        )
        == 250_000
    )


def test_unknown_action(viewer: Profile) -> None:
    with pytest.raises(ValueError, match="unknown action"):
        viewer_ledger(viewer).cost_us("zoom")


def test_time_budget(viewer: Profile) -> None:
    led = viewer_ledger(viewer, max_virtual_s=1.0)
    assert led.remaining() == {"virtual_s": 1.0, "views": None}
    led.check("pan")
    led.charge(to_us(1.0))
    assert led.elapsed_s == 1.0
    assert led.remaining()["virtual_s"] == 0.0
    with pytest.raises(BudgetExhausted) as err:
        led.check("pan")
    assert "time budget" in err.value.message
    for action in BUDGET_EXEMPT:
        led.check(action)


def test_view_budget_only_limits_new_views(viewer: Profile) -> None:
    led = viewer_ledger(viewer, max_views=1)
    led.check("view", serves_view=True)
    led.note_view()
    assert led.remaining() == {"virtual_s": None, "views": 0}
    with pytest.raises(BudgetExhausted, match="view budget"):
        led.check("pan", serves_view=True)
    led.check("measure")  # measuring what was already seen is still allowed
    led.check("get_thumbnail")
    led.check("submit_report")


def test_microsecond_rounding() -> None:
    assert to_us(0.1) + to_us(0.2) == to_us(0.3)
    assert to_s(575_000) == 0.575
