"""Baseline agents (M4) over real HTTP: the serpentine agent through REST and through MCP (SPEC 16),
the random and crop agents, and the pure helpers they use."""

from __future__ import annotations

import io
import json
from typing import Any

import numpy as np
import pytest
from PIL import Image

from slidetwin.agents import random_agent, serpentine_agent, unconstrained_crop_agent
from slidetwin.agents.client import Env, EnvError
from slidetwin.agents.serpentine_agent import plan_serpentine
from slidetwin.agents.unconstrained_crop_agent import pick_blocks
from slidetwin.agents.vision import decode, dense_purple, mask_box, purple_fraction, tissue_mask
from slidetwin.core.manager import SessionManager
from slidetwin.core.replay import replay
from tests.agents.conftest import SECRET
from tests.helpers.synth import PINK, PURPLE, tissue_image

Live = tuple[str, SessionManager]


def _env(url: str, transport: str) -> Env:
    return Env.rest(url) if transport == "rest" else Env.mcp(f"{url}/mcp")


def _events(trajectory: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trajectory.splitlines()[1:]]


# --- helpers ----------------------------------------------------------------------------------


def test_vision_heuristics_on_stain_colours() -> None:
    img = tissue_image(200, 100, [(0, 0, 100, 100, PINK), (120, 20, 40, 30, PURPLE)])
    jpeg = io.BytesIO()
    Image.fromarray(img).save(jpeg, format="JPEG", quality=95)
    rgb = decode(jpeg.getvalue())
    assert tissue_mask(rgb)[50, 50]
    assert not tissue_mask(rgb)[95, 190]  # glass
    assert dense_purple(rgb)[35, 140]
    assert not dense_purple(rgb)[50, 50]  # pink stroma
    assert purple_fraction(rgb) == pytest.approx(1200 / 11200, abs=0.02)
    assert mask_box(dense_purple(rgb)) == pytest.approx((120, 20, 160, 50), abs=2)
    assert mask_box(np.zeros((4, 4), bool)) is None
    assert purple_fraction(np.full((8, 8, 3), 255, np.uint8)) == 0.0


def test_serpentine_plan_visits_tissue_fields_row_by_row() -> None:
    mask = np.zeros((40, 60), bool)
    mask[2:6, 2:8] = True  # field (0, 0)
    mask[25:35, 40:55] = True  # fields in rows 2-3, columns 4-5
    # 10 um per thumbnail px: 600 x 400 um slide, fields of 100 x 100 um.
    plan = plan_serpentine(mask, (10.0, 10.0), (600.0, 400.0), (100.0, 100.0))
    assert plan[0] == (50.0, 50.0)
    rows = [y for _, y in plan]
    assert rows == sorted(rows)
    row2 = [x for x, y in plan if y == 250.0]
    row3 = [x for x, y in plan if y == 350.0]
    assert row2 == sorted(row2)
    assert row3 == sorted(row3, reverse=True)  # alternating direction
    assert (150.0, 150.0) not in plan  # empty field skipped


def test_crop_blocks_prefer_purple_tissue() -> None:
    rgb = tissue_image(64, 32, [(0, 0, 32, 32, PINK), (48, 0, 16, 16, PURPLE)])
    best, second = pick_blocks(rgb, 16, 2)
    assert best == (48, 0)
    assert second[0] < 32  # then a pink block, never glass
    assert pick_blocks(np.full((32, 32, 3), 255, np.uint8), 16, 3) == []


# --- agents through the environment --------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_serpentine_agent_proves_coverage(live: Live, transport: str) -> None:
    url, manager = live
    async with _env(url, transport) as env:
        result = await serpentine_agent.run(
            env, "syn_tissue", viewport_px=[512, 384], actor=f"agent:serpentine-{transport}"
        )
    receipt = result.receipt
    assert receipt is not None
    assert receipt["accepted"] is True
    assert receipt["coverage"]["fraction"] >= 0.98
    checks = receipt["rule_checks"]
    # The purple piece is flagged and examined at 20x; the pink piece gets a coverage proof.
    e2 = [c["status"] for c in checks if c["rule"] == "E2"]
    assert e2 == ["pass"] * 3  # max_checks fields examined at 20x
    assert [(c["claim_id"], c["status"]) for c in checks if c["rule"] == "E4"] == [("n1", "pass")]
    assert receipt["violations"] == 0
    events = _events(result.trajectory)
    actions = [e["action"] for e in events]
    assert actions[:3] == ["create_session", "get_thumbnail", "set_magnification"]
    assert "pan" in actions
    assert "measure" in actions
    assert not [e for e in events if e["error"]]
    assert SECRET not in result.trajectory
    assert replay(result.trajectory, manager.registry).ok
    assert result.notes[0].startswith("scanned")


@pytest.mark.anyio
async def test_random_agent_is_reproducible(live: Live) -> None:
    url, manager = live
    params = []
    for _ in range(2):
        async with Env.rest(url) as env:
            result = await random_agent.run(env, "syn_tissue", seed=7, steps=8)
        events = _events(result.trajectory)
        params.append([(e["action"], e["params"]) for e in events[1:-2]])
        assert result.receipt is not None
        assert {c["rule"] for c in result.receipt["rule_checks"]} == {"E1", "E2", "E3", "E4"}
        assert replay(result.trajectory, manager.registry).ok
    assert params[0] == params[1]
    assert len(params[0]) == 8


@pytest.mark.anyio
async def test_crop_agent_uses_the_unconstrained_profile(live: Live) -> None:
    url, manager = live
    async with Env.mcp(f"{url}/mcp") as env:
        result = await unconstrained_crop_agent.run(
            env, "syn_tissue", crops=3, viewport_px=[512, 384]
        )
    header = json.loads(result.trajectory.splitlines()[0])
    assert header["profile"]["id"] == "unconstrained"
    events = _events(result.trajectory)
    assert [e["action"] for e in events].count("crop") == 3
    assert result.receipt is not None
    finding = next(c for c in result.receipt["rule_checks"] if c["rule"] == "E1")
    assert finding["status"] == "pass"  # crops are cited as evidence like views
    assert replay(result.trajectory, manager.registry).ok


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_refusals_raise_env_errors(live: Live, transport: str) -> None:
    url, _ = live
    async with _env(url, transport) as env:
        assert [s["id"] for s in await env.list_slides()] == ["syn_tissue"]
        assert (await env.report_schema())["title"] == "SlideTwin report"
        s = await env.open("syn_tissue", actor="agent:errors", viewport_px=[512, 384])
        with pytest.raises(EnvError) as info:
            await s.set_magnification(40)
        assert info.value.code == "MAG_UNAVAILABLE"
        assert "20x" in info.value.hint
        with pytest.raises(EnvError) as info:
            await s.crop(0, 0, 10, 10)
        assert info.value.code == "ACTION_NOT_ALLOWED"
        with pytest.raises(EnvError) as info:
            await env.open("missing", actor="agent:errors")
        assert info.value.code == "SLIDE_NOT_FOUND"
        state = (await s.get_state()).state
        assert state["views_served"] == 1
        step = await s.view()
        assert step.image is not None
        assert step.image[:3] == b"\xff\xd8\xff"
        assert (await s.annotate({"type": "point", "coordinates": [5, 5]}, "x")).observation[
            "label"
        ] == "x"
