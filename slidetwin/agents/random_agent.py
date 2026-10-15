"""Random baseline (SPEC 12): random magnifications and moves, then a random conclusion.

A lower bound. Its reports usually fail the rule checks, which is part of what it shows.
"""

from __future__ import annotations

import random

from slidetwin.agents.client import Env, EnvError
from slidetwin.agents.common import AgentResult, finish, report


async def run(
    env: Env,
    slide_id: str,
    *,
    seed: int = 0,
    steps: int = 20,
    actor: str = "agent:random",
    profile: str = "viewer",
    viewport_px: list[int] | None = None,
) -> AgentResult:
    rng = random.Random(seed)
    s = await env.open(slide_id, actor=actor, profile=profile, viewport_px=viewport_px)
    mags = s.slide["available_mags"]
    w, h = s.slide["dimensions_um"]
    notes: list[str] = []
    for _ in range(steps):
        kind = rng.choice(["magnification", "pan", "move_to"])
        try:
            if kind == "magnification":
                await s.set_magnification(rng.choice(mags))
            elif kind == "pan":
                await s.pan(round(rng.uniform(-1, 1), 2), round(rng.uniform(-1, 1), 2))
            else:
                await s.move_to(round(rng.uniform(0, w), 1), round(rng.uniform(0, h), 1))
        except EnvError as err:  # e.g. the time budget ran out: stop moving, still report
            notes.append(f"{kind} refused: {err.code}")
            break
    view_id = s.last.state["current_view_id"]
    if rng.random() < 0.5 and view_id:
        category = rng.choice(["macro", "micro", "itc"])
        claim = {"id": "f1", "type": "finding", "text": "Random pick.", "evidence": [view_id]}
        body = report("present", category, [claim])
    else:
        claim = {"id": "n1", "type": "negative", "text": "Random pick: nothing seen."}
        body = report("absent", "none", [claim])
    return await finish(s, body, notes)
