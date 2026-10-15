"""Cost-model calibration (M7): known costs plus noise are recovered from simulated human logs."""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

import pytest

from slidetwin.core.trajectory import parse_trajectory
from slidetwin.eval.__main__ import main
from slidetwin.eval.calibrate import Interval, fit, intervals, theil_sen
from tests.conftest import SessionFactory

TRUE = {
    "view_render_s": 0.3,
    "set_magnification_s": 1.2,
    "pan_base_s": 0.4,
    "pan_per_viewport_s": 0.8,
    "move_to_s": 1.5,
    "thumbnail_s": 2.0,
    "measure_s": 5.0,
    "annotate_s": 6.0,
}
CURRENT = {"kind": "viewer", **{k: 1.0 for k in TRUE}}
FOV = (1000.0, 800.0)


def _event(
    seq: int, action: str, t_ms: int, centre: tuple[float, float], error: Any = None
) -> dict[str, Any]:
    x, y = centre
    bbox = [x - FOV[0] / 2, y - FOV[1] / 2, x + FOV[0] / 2, y + FOV[1] / 2]
    return {
        "seq": seq,
        "action": action,
        "t_wall_ms": t_ms,
        "state_after": {"center_um": [x, y], "bbox_um": bbox},
        "error": error,
    }


def simulated_session(seed: int, n: int = 120) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A human log whose intervals follow TRUE with 10% noise and an occasional coffee break."""
    rng = random.Random(seed)
    t, centre = 0, (50_000.0, 50_000.0)
    events = [_event(1, "create_session", t, centre)]
    render = TRUE["view_render_s"]
    for seq in range(2, n):
        action = rng.choice(
            ["set_magnification", "pan", "pan", "move_to", "get_thumbnail", "measure", "annotate"]
        )
        if action == "pan":
            dx, dy = rng.uniform(-1, 1), rng.uniform(-1, 1)
            centre = (centre[0] + dx * FOV[0], centre[1] + dy * FOV[1])
            cost = TRUE["pan_base_s"] + TRUE["pan_per_viewport_s"] * math.hypot(dx, dy) + render
        elif action == "move_to":
            centre = (rng.uniform(10_000, 90_000), rng.uniform(10_000, 90_000))
            cost = TRUE["move_to_s"] + render
        elif action == "set_magnification":
            cost = TRUE["set_magnification_s"] + render
        else:
            cost = TRUE[
                {"get_thumbnail": "thumbnail_s", "measure": "measure_s", "annotate": "annotate_s"}[
                    action
                ]
            ]
        cost *= rng.gauss(1.0, 0.1)
        if rng.random() < 0.03:
            cost += 300  # a break: longer than max_gap_s, left out
        t += round(cost * 1000)
        events.append(_event(seq, action, t, centre))
    events.append(
        _event(n, "pan", t + 700, centre, error={"code": "OUTSIDE_VIEW"})
    )  # failed: skipped
    return {"actor": "human:sim"}, events


def test_theil_sen_ignores_outliers() -> None:
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [1.0, 3.0, 5.0, 7.0, 100.0, 11.0]
    base, slope = theil_sen(xs, ys)
    assert slope == pytest.approx(2.0)
    assert base == pytest.approx(1.0)
    assert theil_sen([1.0, 1.0], [2.0, 4.0]) == (3.0, 0.0)


def test_known_costs_are_recovered() -> None:
    sessions = [simulated_session(seed) for seed in range(6)]
    result = fit(sessions, CURRENT, n_boot=100)
    assert result.sessions == 6
    assert result.params["view_render_s"] == 1.0  # no `view` actions: kept as given
    fitted = fit(sessions, {**CURRENT, "view_render_s": 0.3}, n_boot=100)
    for key, true in TRUE.items():
        assert fitted.params[key] == pytest.approx(true, rel=0.15, abs=0.08), key
        if key == "view_render_s":
            assert key not in fitted.ci95  # given, not fitted: no interval
            continue
        lo, hi = fitted.ci95[key]
        assert lo <= fitted.params[key] <= hi
    assert "pan_per_viewport_s" in fitted.yaml()
    assert fitted.markdown().count("\n") == 2 + len(TRUE)


def test_breaks_automatic_actions_failures_and_agents_give_no_intervals() -> None:
    header = {"actor": "human:sim"}
    centre = (50_000.0, 50_000.0)
    events = [
        _event(1, "create_session", 0, centre),
        {**_event(2, "get_thumbnail", 120, centre), "client": {"input": "auto"}},  # by the UI
        _event(3, "measure", 5_120, centre),  # 5 s
        _event(4, "measure", 305_120, centre),  # a 300 s break
        _event(5, "pan", 306_000, centre, error={"code": "OUTSIDE_VIEW"}),  # failed
        _event(6, "measure", 310_000, centre),  # 4 s after the failed attempt
    ]
    got = intervals(header, events, max_gap_s=60)
    assert [(iv.action, iv.seconds) for iv in got] == [("measure", 5.0), ("measure", 4.0)]
    assert len(intervals(header, events, max_gap_s=1e9)) == 3  # the break, if allowed
    assert intervals({"actor": "agent:x"}, events) == []
    assert intervals(header, []) == []
    _, sim = simulated_session(0, n=10)
    empty = fit([({"actor": "agent:x"}, sim)], CURRENT)
    assert empty.sessions == 0
    assert empty.params == {k: v for k, v in CURRENT.items() if k != "kind"}
    assert Interval("pan", 1.0).viewports == 0.0


def test_a_real_session_log_has_the_fields_the_fit_needs(make_session: SessionFactory) -> None:
    clock = [1_000_000]

    def wall() -> int:
        return clock[0]

    s = make_session(actor="human:p1", wall_ms=wall)
    for action, step_ms in [
        (lambda: s.get_thumbnail(), 2000),
        (lambda: s.set_magnification(10), 1500),
        (lambda: s.pan(0.5, 0), 800),
        (lambda: s.move_to(700, 600), 1800),
    ]:
        clock[0] += step_ms
        action()
    header, events = parse_trajectory(s.export_trajectory().text or "")
    got = intervals(header, events)
    assert [(iv.action, iv.seconds) for iv in got] == [
        ("get_thumbnail", 2.0),
        ("set_magnification", 1.5),
        ("pan", 0.8),
        ("move_to", 1.8),
    ]
    assert got[2].viewports == pytest.approx(0.5)


def test_command_line(
    make_session: SessionFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [0]
    s = make_session(actor="human:p2", wall_ms=lambda: clock[0])
    for _ in range(3):
        clock[0] += 1000
        s.get_thumbnail()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.jsonl").write_text(s.export_trajectory().text or "")
    fitted = tmp_path / "fitted.yaml"
    main(["calibrate", str(logs), "--fitted", str(fitted)])
    assert "| thumbnail_s | 3 | 1.00 |" in capsys.readouterr().out
    assert "thumbnail_s: 1.0" in fitted.read_text()
    agent = make_session(actor="agent:z", session_id="s-agent")
    (logs / "a.jsonl").write_text(agent.export_trajectory().text or "")
    with pytest.raises(SystemExit):
        main(["calibrate", str(logs)])
    with pytest.raises(SystemExit):
        main(["calibrate"])
